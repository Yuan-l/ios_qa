#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码指纹：复制粘贴 / 近似重复检测。

重复代码要判断的是「结构是不是一样」，不是「语义像不像」，所以这里不用
向量，用经典的词法指纹，纯 stdlib、结果可复现：

- **归一化**：去注释、字符串统一成 ``str``、数字统一成 ``num``、标识符按
  驼峰/下划线拆成小写段。改局部变量名不会让两段代码脱钩，但换掉调用的
  方法名会。
- **精确重复**：归一化 token 序列的 sha1（空白、注释、字面量都不影响）。
- **近似重复**：token 4-gram + MinHash（16 个排列）+ 分带 LSH 出候选，
  候选再用 shingle 集合算**精确 Jaccard** 复核。MinHash 只负责找候选，
  报告出来的 similarity 是真实集合相似度，不是估计值。
- 太短的方法不做指纹：``super.init()`` 这类样板没有信息量，全仓库都一样。

指纹存放在 ``code_fingerprints`` 表，按 ``(file_path, line_start, method_name)``
与 ``methods`` 对齐；body 变了由 ``rebuild_fingerprints`` 增量补算。
"""

from __future__ import annotations

import hashlib
import re
import struct
import zlib
from dataclasses import dataclass, field

from .source import mask_comments

SHINGLE_SIZE = 4
SIG_LEN = 16
BAND_SIZE = 2
BANDS = SIG_LEN // BAND_SIZE
MIN_TOKENS = 24

# Swift 的多行字符串先整体折叠；单行字符串交给下面的 token 正则
_ML_STRING_RE = re.compile(r'"""(?:\\.|(?!""").)*"""', re.S)
_TOKEN_RE = re.compile(
    r'@?"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'"
    r"|[A-Za-z_][A-Za-z0-9_]*"
    r"|\d+(?:\.\d+)?"
    r"|[^\sA-Za-z0-9_]"
)
_IDENT_PART_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")

# 16 个排列的 (a, b)，写死保证任何机器上指纹一致
_MOD = (1 << 61) - 1
_SIG_A = tuple(((0x9E3779B97F4A7C15 * (k + 1)) % _MOD) or 1 for k in range(SIG_LEN))
_SIG_B = tuple((0xD1B54A32D192ED03 * (k + 7)) % _MOD for k in range(SIG_LEN))


@dataclass
class Fingerprint:
    token_count: int
    shingle_count: int
    norm_hash: str
    signature: bytes  # SIG_LEN 个 uint32，Little-Endian
    shingles: bytes  # zlib 压缩后的排序 uint32 列表
    shingle_ids: set[int] = field(default_factory=set, repr=False)

    def ids(self) -> set[int]:
        if not self.shingle_ids:
            raw = zlib.decompress(self.shingles)
            self.shingle_ids = set(struct.unpack(f"<{len(raw) // 4}I", raw))
        return self.shingle_ids


def _ident_parts(token: str) -> list[str]:
    """``TBFinanceHomeViewController`` -> ``tb finance home view controller``。"""
    out: list[str] = []
    for chunk in token.split("_"):
        if not chunk:
            continue
        parts = _IDENT_PART_RE.findall(chunk)
        for part in parts or [chunk]:
            if len(part) > 1:  # 单字母循环变量改名不应改变指纹
                out.append(part.lower())
    return out


def code_tokens(body: str, language: str = "objc") -> list[str]:
    """把方法体切成归一化 token 序列（保留重复，供 n-gram 使用）。"""
    if not body:
        return []
    text = mask_comments(body, language or "objc")
    text = _ML_STRING_RE.sub(" str ", text)
    out: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        head = token[0]
        if head == "@":
            # ObjC 的 @"..." 是字符串字面量；@selector / @interface 才按标识符拆
            rest = token[1:]
            if rest.startswith('"'):
                out.append("str")
            else:
                out.extend(_ident_parts(rest))
        elif head in "\"'":
            out.append("str")
        elif head.isdigit():
            out.append("num")
        elif head.isalpha() or head == "_":
            out.extend(_ident_parts(token))
        else:
            out.append(token)
    return out


def shingles(tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    if len(tokens) <= SHINGLE_SIZE:
        return ["|".join(tokens)]
    return [
        "|".join(tokens[i : i + SHINGLE_SIZE])
        for i in range(len(tokens) - SHINGLE_SIZE + 1)
    ]


def _hash64(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


def signature(shingle_list: list[str]) -> bytes:
    """MinHash 签名：每个排列在 shingle 集合上的最小值。"""
    values = [_MOD] * SIG_LEN
    for shingle in shingle_list:
        hashed = _hash64(shingle)
        for index in range(SIG_LEN):
            value = (_SIG_A[index] * hashed + _SIG_B[index]) % _MOD
            if value < values[index]:
                values[index] = value
    return struct.pack(f"<{SIG_LEN}I", *[value & 0xFFFFFFFF for value in values])


def fingerprint(body: str, language: str = "objc") -> Fingerprint | None:
    """算一段方法体的指纹；信息量不足（token 太少）时返回 ``None``。"""
    tokens = code_tokens(body, language)
    if len(tokens) < MIN_TOKENS:
        return None
    grams = shingles(tokens)
    ids = sorted({zlib.crc32(gram.encode()) for gram in grams})
    return Fingerprint(
        token_count=len(tokens),
        shingle_count=len(ids),
        norm_hash=hashlib.sha1(" ".join(tokens).encode()).hexdigest()[:20],
        signature=signature(grams),
        shingles=zlib.compress(struct.pack(f"<{len(ids)}I", *ids), 6),
    )


def decode_shingles(blob: bytes) -> set[int]:
    raw = zlib.decompress(blob)
    return set(struct.unpack(f"<{len(raw) // 4}I", raw))


def decode_signature(blob: bytes) -> tuple[int, ...]:
    return struct.unpack(f"<{SIG_LEN}I", blob)


def jaccard(left: set[int], right: set[int]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    return intersection / (len(left) + len(right) - intersection)


def band_keys(sig: bytes) -> list[tuple[int, tuple[int, ...]]]:
    """把签名切成 ``BANDS`` 个带，用于 LSH 出候选。"""
    values = decode_signature(sig)
    return [
        (index, values[index * BAND_SIZE : (index + 1) * BAND_SIZE])
        for index in range(BANDS)
    ]
