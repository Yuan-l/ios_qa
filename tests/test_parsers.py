#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ObjC / Swift / Storyboard 解析器测试（行号必须准确）。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ios_qa.config import ProjectProfile
from ios_qa.discover import SourceFile
from ios_qa.parsers import objc, swift, ui


def make_source(root: Path, rel: str, content: str) -> SourceFile:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    stat = path.stat()
    return SourceFile(
        path=path,
        rel_path=rel,
        module="Demo",
        language="swift" if rel.endswith(".swift") else "objc",
        size=stat.st_size,
        mtime=stat.st_mtime,
        sha256="",
        loc=content.count("\n") + 1,
    )


class ParserTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.profile = ProjectProfile(name="Demo", root=self.root, source_dirs=["."])

    def tearDown(self) -> None:
        self._tmp.cleanup()


class ObjCTest(ParserTestBase):
    HEADER = """\
#import <Foundation/Foundation.h>
@class TBRequestManager;

@interface TBWidget : NSObject
/// 加载首页数据
- (void)loadHomeData:(NSString *)url;
@end
"""

    IMPL = """\
#import "TBWidget.h"

@implementation TBWidget

- (void)loadHomeData:(NSString *)url {
    [[TBRequestManager shared] get:url parameters:nil];
    [[NSNotificationCenter defaultCenter] addObserver:self
                                             selector:@selector(onChange:)
                                                 name:TBDidChangeNotification
                                               object:nil];
}

- (void)onChange:(NSNotification *)note {
    self.title = @"changed";
}

@end
"""

    def test_header_symbol_and_method_line(self) -> None:
        source = make_source(self.root, "TBWidget.h", self.HEADER)
        result = objc.parse_header(self.HEADER, source, self.profile)
        self.assertEqual([s.class_name for s in result.symbols], ["TBWidget"])
        symbol = result.symbols[0]
        self.assertEqual(symbol.kind, "class")
        self.assertEqual(symbol.superclass, "NSObject")
        self.assertEqual(symbol.line_start, 4)
        method = next(m for m in result.methods if m.method_name == "loadHomeData:")
        self.assertEqual(method.line_start, 6)
        self.assertFalse(method.has_body)
        self.assertEqual(method.doc_comment, "加载首页数据")

    def test_impl_methods_calls_and_notifications(self) -> None:
        source = make_source(self.root, "TBWidget.m", self.IMPL)
        result = objc.parse_implementation(self.IMPL, source, self.profile)
        symbol = next(s for s in result.symbols if s.kind == "implementation")
        self.assertEqual(symbol.line_start, 3)

        method = next(m for m in result.methods if m.method_name == "loadHomeData:")
        self.assertEqual((method.line_start, method.line_end), (5, 11))
        self.assertTrue(method.has_body)

        call = next(c for c in result.calls if c.callee_name == "get")
        self.assertEqual(call.callee_class, "TBRequestManager")
        self.assertEqual(call.line_number, 6)

        notification = result.notifications[0]
        self.assertEqual(notification.notification_name, "TBDidChangeNotification")
        self.assertEqual(notification.register_line, 7)
        self.assertEqual(notification.register_method, "loadHomeData:")

        self.assertNotIn("TBWidget", {d.dep_class for d in result.deps if d.dep_kind == "instantiate"})

    def test_line_numbers_match_source(self) -> None:
        lines = self.IMPL.splitlines()
        source = make_source(self.root, "TBWidget.m", self.IMPL)
        result = objc.parse_implementation(self.IMPL, source, self.profile)
        call = next(c for c in result.calls if c.callee_name == "get")
        self.assertIn("TBRequestManager", lines[call.line_number - 1])

    def test_block_and_delegate_calls_from_header(self) -> None:
        header = """\
#import <Foundation/Foundation.h>
@protocol TBWidgetDelegate <NSObject>
- (void)widgetDidSelect:(NSInteger)index;
@end

@interface TBWidget : NSObject
@property (nonatomic, copy) void (^onSelect)(NSInteger index);
@property (nonatomic, weak) id<TBWidgetDelegate> delegate;
@end
"""
        impl = """\
#import "TBWidget.h"

@implementation TBWidget

- (void)tapped:(NSInteger)index {
    if (self.onSelect) {
        self.onSelect(index);
    }
    [self.delegate widgetDidSelect:index];
}

@end
"""
        source = make_source(self.root, "TBWidget.m", impl)
        result = objc.parse_implementation(
            impl, source, self.profile, header_text=header
        )
        block = next(c for c in result.calls if c.kind == "block")
        self.assertEqual((block.callee_class, block.callee_name), ("TBWidget", "onSelect"))
        self.assertEqual(block.line_number, 7)

        delegate = next(c for c in result.calls if c.kind == "delegate")
        self.assertEqual(delegate.callee_class, "TBWidgetDelegate")
        self.assertEqual(delegate.callee_name, "widgetDidSelect")
        self.assertEqual(delegate.line_number, 9)


class SwiftTest(ParserTestBase):
    SOURCE = """\
final class TBHomeViewController: UIViewController {
    private let service = TBHomeService()

    override func viewDidLoad() {
        super.viewDidLoad()
        TBMonitor.shared.markFirstFrame()
        let detail = TBDetailViewController()
        navigationController?.pushViewController(detail, animated: true)
        NotificationCenter.default.addObserver(self, selector: #selector(onChange(_:)), name: .TBDidChange, object: nil)
    }

    @objc private func onChange(_ note: Notification) {}

    private func loadData() {
        _ = URL(string: "https://example.com/api/home")
        TBRequestManager.shared.get(urlString: "https://example.com/api/home")
    }
}

final class TBDetailViewController: UIViewController {}
"""

    def test_types_properties_and_methods(self) -> None:
        source = make_source(self.root, "TBHomeViewController.swift", self.SOURCE)
        result = swift.parse_swift(self.SOURCE, source, self.profile)
        names = {s.class_name for s in result.symbols}
        self.assertIn("TBHomeViewController", names)
        self.assertIn("TBDetailViewController", names)

        home = next(s for s in result.symbols if s.class_name == "TBHomeViewController")
        self.assertEqual(home.superclass, "UIViewController")
        self.assertEqual(home.line_start, 1)
        self.assertIn({"name": "service", "type": "TBHomeService"}, home.properties)

        load = next(m for m in result.methods if m.method_name == "loadData")
        self.assertEqual(load.line_start, 14)

    def test_calls_and_navigation_line_numbers(self) -> None:
        source = make_source(self.root, "TBHomeViewController.swift", self.SOURCE)
        result = swift.parse_swift(self.SOURCE, source, self.profile)
        lines = self.SOURCE.splitlines()

        call = next(c for c in result.calls if c.callee_name == "markFirstFrame")
        self.assertEqual(call.callee_class, "TBMonitor")
        self.assertEqual(call.line_number, 6)
        self.assertIn("TBMonitor.shared", lines[call.line_number - 1])

        nav = next(n for n in result.navigation if n.kind == "push")
        self.assertEqual(nav.to_class, "TBDetailViewController")
        self.assertEqual(nav.line_number, 8)

        notification = next(
            n for n in result.notifications if n.notification_name == "TBDidChange"
        )
        self.assertEqual(notification.register_line, 9)

    def test_bare_root_view_controller_resolved(self) -> None:
        text = """\
final class TBAppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?
    func application(_ application: UIApplication) -> Bool {
        window?.rootViewController = TBHomeViewController()
        return true
    }
}
final class TBHomeViewController: UIViewController {}
"""
        source = make_source(self.root, "TBAppDelegate.swift", text)
        result = swift.parse_swift(text, source, self.profile)
        nav = next(n for n in result.navigation if n.kind == "root")
        self.assertEqual(nav.to_class, "TBHomeViewController")
        self.assertEqual(nav.line_number, 4)

    def test_api_url_literal(self) -> None:
        source = make_source(self.root, "TBHomeViewController.swift", self.SOURCE)
        result = swift.parse_swift(self.SOURCE, source, self.profile)
        urls = {a.url_pattern for a in result.api_calls}
        self.assertIn("https://example.com/api/home", urls)
        self.assertTrue(all("\n" not in u for u in urls))

    def test_tab_container_and_multiline_class(self) -> None:
        text = """\
final class TBAppDelegate: UIResponder,
    UIApplicationDelegate {
    func application() -> Bool {
        let tab = TBTabbarViewController()
        let home = TBHomeViewController()
        tab.viewControllers?.append(UINavigationController(rootViewController: home))
        let detail = TBDetailViewController()
        tab.viewControllers = [UINavigationController(rootViewController: detail)]
        return true
    }
}

final class TBTabbarViewController: UITabBarController {}

final class TBHomeViewController: UIViewController {}

final class TBDetailViewController: UIViewController {}
"""
        source = make_source(self.root, "TBAppDelegate.swift", text)
        result = swift.parse_swift(text, source, self.profile)
        app = next(s for s in result.symbols if s.class_name == "TBAppDelegate")
        self.assertEqual(app.superclass, "UIResponder")
        self.assertIn("UIApplicationDelegate", app.protocols)

        tab_targets = {(n.to_class, n.line_number) for n in result.navigation if n.kind == "tab"}
        self.assertIn(("TBHomeViewController", 6), tab_targets)
        self.assertIn(("TBDetailViewController", 8), tab_targets)

    def test_delegate_and_block_dispatch(self) -> None:
        text = """\
protocol TBHomeDelegate: AnyObject {
    func didSelect(index: Int)
}

final class TBHomeViewController: UIViewController {
    weak var delegate: TBHomeDelegate?
    var onSelect: ((Int) -> Void)?

    func select(index: Int) {
        delegate?.didSelect(index: index)
        onSelect?(index)
    }
}

extension TBHomeViewController {
    func reload() {
        self.delegate?.didSelect(index: 0)
    }
}
"""
        source = make_source(self.root, "TBHomeViewController.swift", text)
        result = swift.parse_swift(text, source, self.profile)
        delegates = [c for c in result.calls if c.kind == "delegate"]
        self.assertEqual(
            {(c.callee_class, c.callee_name) for c in delegates},
            {("TBHomeDelegate", "didSelect")},
        )
        # 主类 + 同文件 extension 都要能解析（属性声明在主类）
        self.assertEqual({c.line_number for c in delegates}, {10, 17})

        block = next(c for c in result.calls if c.kind == "block")
        self.assertEqual((block.callee_class, block.callee_name), ("TBHomeViewController", "onSelect"))
        self.assertEqual(block.line_number, 11)


class UITest(ParserTestBase):
    STORYBOARD = """\
<?xml version="1.0" encoding="UTF-8"?>
<document type="com.apple.InterfaceBuilder3.CocoaTouch.Storyboard.XIB">
  <scenes>
    <scene>
      <objects>
        <viewController id="abc-123" customClass="TBHomeViewController" storyboardIdentifier="Home">
          <connections>
            <segue destination="def-456" kind="show" identifier="toDetail" id="seg-1"/>
          </connections>
        </viewController>
      </objects>
    </scene>
  </scenes>
</document>
"""

    def test_entrypoint_and_segue(self) -> None:
        source = make_source(self.root, "Main.storyboard", self.STORYBOARD)
        result = ui.parse_ui(self.STORYBOARD, source, self.profile)
        self.assertEqual(len(result.entrypoints), 1)
        entry = result.entrypoints[0]
        self.assertEqual(entry.class_name, "TBHomeViewController")
        self.assertEqual(entry.identifier, "Home")
        self.assertEqual(entry.kind, "storyboard")
        self.assertEqual(len(result.navigation), 1)
        self.assertEqual(result.navigation[0].identifier, "toDetail")


if __name__ == "__main__":
    unittest.main()
