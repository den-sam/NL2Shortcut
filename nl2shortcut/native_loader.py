"""C++ 原生 DLL 的 ctypes 封装层。

编译 nl2shortcut_native.dll 后，本模块自动发现并加载。
如果 DLL 不存在，所有函数静默返回 None/fallback，不抛异常。

用法
----
    from nl2shortcut.native_loader import native

    if native.available:
        json_str = native.uia_snapshot(max_depth=12, max_nodes=500)
        native.send_hotkey("Ctrl+C")
"""

from __future__ import annotations

import ctypes
import json
import os
from typing import Optional, Dict, Any


class _NativeDLL:
    """ctypes DLL 封装 —— 所有函数返回 Python 友好的结果。"""

    # 快捷键执行层必需的导出函数；缺任意一个都视为 DLL 不可用。
    _REQUIRED = ("send_hotkey", "free_result")

    def __init__(self):
        self._dll = None
        self._missing: list = []
        self._path: Optional[str] = None
        self._load()

    # ── 加载 ────────────────────────────────────────────────────────

    def _load(self):
        """按优先级查找 DLL 并加载。

        容错策略：遇到损坏 / 架构不匹配的 DLL 候选（如包目录里残留的占位文件），
        静默跳过继续尝试下一个搜索路径，而不是直接抛 OSError 让调用方崩溃。
        典型损坏场景：`STATUS_INVALID_IMAGE_FORMAT` (WinError 193 / 0xC000012F)，
        是因为搜索路径命中了一个 12 字节的占位文本文件而非真正的 PE。
        """
        search_paths = [
            # 1. 当前包目录（pip install 后）
            os.path.join(os.path.dirname(__file__), "nl2shortcut_native.dll"),
            # 2. native_core/output/Release（CMake 输出）
            os.path.join(os.path.dirname(__file__), "native_core", "output", "Release",
                         "nl2shortcut_native.dll"),
            # 3. native_core/output（直接编译输出）
            os.path.join(os.path.dirname(__file__), "native_core", "output",
                         "nl2shortcut_native.dll"),
            # 4. CWD
            os.path.join(os.getcwd(), "nl2shortcut_native.dll"),
        ]

        for path in search_paths:
            if not os.path.isfile(path):
                continue
            # 快速健康检查：有效 PE 文件应 ≥ 1KB 且以 MZ 头开头
            try:
                with open(path, "rb") as f:
                    head = f.read(2)
                if len(head) < 2 or head != b"MZ":
                    continue  # 占位文件 / 损坏文件，跳过
            except OSError:
                continue

            try:
                dll = ctypes.WinDLL(path)
            except OSError:
                # 架构不匹配 / 文件已损坏 / 找不到依赖 → 尝试下一个候选
                continue

            # 校验必需符号确实存在。
            # `extern "C"` 只去除 name mangling，符号要进入 DLL 导出表还需
            # __declspec(dllexport)；漏掉时 DLL 能加载但每个函数都取不到。
            # 这里显式校验，避免「available=True 但调用全部静默失败」。
            missing = [n for n in self._REQUIRED if not hasattr(dll, n)]
            if missing:
                self._missing = missing
                continue
            self._dll = dll
            self._path = path
            self._setup_signatures()
            return

    def _setup_signatures(self):
        """设置所有导出函数的参数和返回类型。"""
        if self._dll is None:
            return

        # ── uia_snapshot ─────────────────────────────────────────
        # const char* uia_snapshot(int max_depth, int max_nodes);
        # restype 必须是 c_void_p：若用 c_char_p，ctypes 会**自动复制字符串并
        # 立即释放原文内存**，导致传回 free_result 的指针已成悬空指针 → 堆损坏
        # (0xC0000374)。用 void* 返回原始指针，由我们手动 CoTaskMemFree 才安全。
        try:
            fn = self._dll.uia_snapshot
            fn.argtypes = [ctypes.c_int, ctypes.c_int]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass

        # ── uia_diff ────────────────────────────────────────────
        # const char* uia_diff(const char* before_json, const char* after_json);
        # 同样用 c_void_p 返回原始指针。
        try:
            fn = self._dll.uia_diff
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass

        # ── uia_diff_filtered ──────────────────────────────────
        # const char* uia_diff_filtered(const char* before, const char* after,
        #                               int filter_flags, int position_tolerance_px);
        try:
            fn = self._dll.uia_diff_filtered
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                           ctypes.c_int, ctypes.c_int]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass

        # ── execute_with_retry ─────────────────────────────────
        # const char* execute_with_retry(const char* candidates_json,
        #                                int verify_delay_ms, int max_attempts,
        #                                int use_clipboard_check, int use_window_check);
        try:
            fn = self._dll.execute_with_retry
            fn.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass

        # ── send_hotkey ──────────────────────────────────────────
        # int send_hotkey(const char* key_combination);
        try:
            fn = self._dll.send_hotkey
            fn.argtypes = [ctypes.c_char_p]
            fn.restype = ctypes.c_int
        except AttributeError:
            pass

        # ── send_unicode_char ────────────────────────────────────
        # int send_unicode_char(const wchar_t* text);
        try:
            fn = self._dll.send_unicode_char
            fn.argtypes = [ctypes.c_wchar_p]
            fn.restype = ctypes.c_int
        except AttributeError:
            pass

        # ── type_via_clipboard ───────────────────────────────────
        # int type_via_clipboard(const wchar_t* text);
        try:
            fn = self._dll.type_via_clipboard
            fn.argtypes = [ctypes.c_wchar_p]
            fn.restype = ctypes.c_int
        except AttributeError:
            pass

        # ── validate_hotkey ──────────────────────────────────────
        # int validate_hotkey(const char* key_combination);
        try:
            fn = self._dll.validate_hotkey
            fn.argtypes = [ctypes.c_char_p]
            fn.restype = ctypes.c_int
        except AttributeError:
            pass

        # ── free_result ──────────────────────────────────────────
        # void free_result(void* ptr);
        try:
            fn = self._dll.free_result
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = None
        except AttributeError:
            pass

        # ── foreground_window_json ───────────────────────────────────────
        # const char* foreground_window_json();
        # 同样用 c_void_p 返回原始指针，避免 c_char_p 自动释放导致 free_result 崩溃。
        try:
            fn = self._dll.foreground_window_json
            fn.argtypes = []
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass

        # ── fuzzy_match 模块 ───────────────────────────────────────────
        # void* ac_build(const char* keywords_json);
        # const char* ac_contains(void* handle, const char* text);
        # void ac_free(void* handle);
        # double fuzzy_ratio(const char* a, const char* b);
        # const char* fuzzy_best_match(const char* kws, const char* text, double t);
        try:
            fn = self._dll.ac_build
            fn.argtypes = [ctypes.c_char_p]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass
        try:
            fn = self._dll.ac_contains
            fn.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass
        try:
            fn = self._dll.ac_free
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = None
        except AttributeError:
            pass
        try:
            fn = self._dll.fuzzy_ratio
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
            fn.restype = ctypes.c_double
        except AttributeError:
            pass
        try:
            fn = self._dll.fuzzy_best_match
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_double]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass

        # ── scache 模块 ─────────────────────────────────────────────────
        # SemanticCacheNative* scache_create(int max_entries, double fuzzy_threshold);
        # void scache_free(SemanticCacheNative* sc);
        # int scache_set(SemanticCacheNative* sc, const char* key, const char* intent,
        #                const char* value_json, double ttl_seconds);
        # const char* scache_get(SemanticCacheNative* sc, const char* key);
        # const char* scache_fuzzy_lookup(SemanticCacheNative* sc, const char* intent);
        # void scache_clear(SemanticCacheNative* sc);
        # int scache_size(SemanticCacheNative* sc);
        try:
            fn = self._dll.scache_create
            fn.argtypes = [ctypes.c_int, ctypes.c_double]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass
        try:
            fn = self._dll.scache_free
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = None
        except AttributeError:
            pass
        try:
            fn = self._dll.scache_set
            fn.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                           ctypes.c_char_p, ctypes.c_double]
            fn.restype = ctypes.c_int
        except AttributeError:
            pass
        try:
            fn = self._dll.scache_get
            fn.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass
        try:
            fn = self._dll.scache_fuzzy_lookup
            fn.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass
        try:
            fn = self._dll.scache_clear
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = None
        except AttributeError:
            pass
        try:
            fn = self._dll.scache_size
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_int
        except AttributeError:
            pass

        # ── context_proc 模块 ──────────────────────────────────────────
        # const char* get_process_name_by_pid(uint32_t pid);
        # const char* get_foreground_context();
        # const char* fingerprint_process(const char* process_name);
        try:
            fn = self._dll.get_process_name_by_pid
            fn.argtypes = [ctypes.c_uint32]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass
        try:
            fn = self._dll.get_foreground_context
            fn.argtypes = []
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass
        try:
            fn = self._dll.fingerprint_process
            fn.argtypes = [ctypes.c_char_p]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass

        # ── pattern_cluster 模块 ───────────────────────────────────────
        # const char* cluster_operations(const char* records_json,
        #                                int gap, int min_len, int min_freq);
        # const char* canonical_key(const char* detail);
        try:
            fn = self._dll.cluster_operations
            fn.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass
        try:
            fn = self._dll.canonical_key
            fn.argtypes = [ctypes.c_char_p]
            fn.restype = ctypes.c_void_p
        except AttributeError:
            pass

    def _consume_cstr(self, ptr) -> Optional[str]:
        """读取 C 字符串指针并自动 free_result；空指针返回 None。
        注意：空字符串 "" 是合法返回值，不等于 None。"""
        if not ptr:
            return None
        raw = ctypes.cast(ptr, ctypes.c_char_p).value
        # raw 可能是 b""（空字符串，合法）或 None（理论上不应发生）
        s = raw.decode("utf-8") if raw is not None else ""
        self._dll.free_result(ptr)
        return s

    # ── 属性 ────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._dll is not None

    @property
    def path(self) -> Optional[str]:
        """已加载 DLL 的绝对路径；未加载为 None。"""
        return self._path

    @property
    def missing_symbols(self) -> list:
        """曾因缺失而导致 DLL 被拒绝的符号名（用于排查构建问题）。"""
        return list(self._missing)

    # ── API ─────────────────────────────────────────────────────────

    def uia_snapshot(self, max_depth: int = 12, max_nodes: int = 500) -> Optional[Dict[str, Any]]:
        """采集 UIA 树快照，返回 Python dict。失败返回 None。"""
        if self._dll is None:
            return None
        try:
            ptr = self._dll.uia_snapshot(max_depth, max_nodes)
            if not ptr:
                return None
            # ptr 是 c_void_p（原始指针），ctypes 不会自动释放，需手动读 + 释放。
            raw = ctypes.cast(ptr, ctypes.c_char_p).value
            result = json.loads(raw.decode("utf-8")) if raw else None
            # 释放 C++ 分配的内存
            self._dll.free_result(ptr)
            return result
        except Exception:
            return None

    def uia_diff(self, before_json: str, after_json: str) -> Optional[Dict[str, Any]]:
        """对比两棵 UIA 树 JSON 快照，返回节点级 diff（不过滤任何差异）。

        若需要自动过滤焦点变化/位置抖动/元字段差异，请改用 uia_diff_filtered()。

        Args:
            before_json: uia_snapshot() 返回的 JSON 字符串（注入前）
            after_json:  uia_snapshot() 返回的 JSON 字符串（注入后）
        Returns:
            {"changed":[...], "added":[...], "removed":[...], "summary":{...}}
            失败返回 None。
        """
        if self._dll is None:
            return None
        try:
            ptr = self._dll.uia_diff(
                before_json.encode("utf-8"),
                after_json.encode("utf-8"),
            )
            if not ptr:
                return None
            raw = ctypes.cast(ptr, ctypes.c_char_p).value
            result = json.loads(raw.decode("utf-8")) if raw else None
            self._dll.free_result(ptr)
            return result
        except Exception:
            return None

    # 过滤标志位（与 native_core/src/uia.h 的 UIA_FILTER_* enum 保持一致）
    UIA_FILTER_FOCUS_STATE = 0x01   # 忽略 state 中 focused/focusable 相关变化
    UIA_FILTER_POSITION_PX = 0x02   # 忽略位置/尺寸变化 < position_tolerance 像素
    UIA_FILTER_META_FIELDS = 0x04   # 忽略 elapsed_ms / focus 顶层元信息字段
    UIA_FILTER_ALL         = 0xFF   # 启用全部非关键过滤（推荐默认）

    def uia_diff_filtered(
        self,
        before_json: str,
        after_json: str,
        filter_flags: int = UIA_FILTER_ALL,
        position_tolerance_px: int = 10,
    ) -> Optional[Dict[str, Any]]:
        """带过滤器的 UIA diff（推荐用于真实窗口对比）。

        自动过滤三类常见的非关键状态差异：
          1. 焦点状态差异（UIA_FILTER_FOCUS_STATE，默认开）
             只在两次采集间焦点在不同控件间转移/变为 focused 的变化；
          2. 位置尺寸抖动（UIA_FILTER_POSITION_PX，默认开，容忍 < 10px）
             窗口 resize/位置微调引起的 1px~10px 尺寸或位置差异；
          3. 快照元字段（UIA_FILTER_META_FIELDS，默认开）
             elapsed_ms / focus 顶层字段（非树外元信息，不在子节点内）。

        Args:
            before_json: uia_snapshot() 返回的 JSON 字符串
            after_json:  uia_snapshot() 返回的 JSON 字符串
            filter_flags: 过滤位掩码，UIA_FILTER_* 组合（默认全部启用）
            position_tolerance_px: 位置/尺寸容忍像素阈值（默认 10）
        Returns:
            summary 额外包含 filtered_focus/filtered_position/filtered_meta 统计
            失败返回 None。
        """
        if self._dll is None:
            return None
        try:
            ptr = self._dll.uia_diff_filtered(
                before_json.encode("utf-8"),
                after_json.encode("utf-8"),
                int(filter_flags),
                int(position_tolerance_px),
            )
            if not ptr:
                return None
            raw = ctypes.cast(ptr, ctypes.c_char_p).value
            result = json.loads(raw.decode("utf-8")) if raw else None
            self._dll.free_result(ptr)
            return result
        except Exception:
            return None

    def execute_with_retry(
        self,
        candidates: list,
        verify_delay_ms: int = 100,
        max_attempts: int = 3,
        use_clipboard_check: bool = False,
        use_window_check: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """C++ 执行内核：候选键构建 + 执行循环 + 验证 + 重试整体下沉到 C++。

        Args:
            candidates: 候选键列表，如 ["Ctrl+C", "Ctrl+Insert"]
            verify_delay_ms: 每次注入后的验证等待时间（毫秒）
            max_attempts: 最大重试次数（含首次）
            use_clipboard_check: 启用剪贴板验证
            use_window_check: 启用前台窗口验证
        Returns:
            {
              "success": bool, "used_key": str, "attempts": int,
              "error": str|None, "elapsed_ms": float,
              "verifications": [{"key","attempt","verified","reason"}]
            }
            失败返回 None（DLL 不可用）。
        """
        if self._dll is None:
            return None
        try:
            candidates_json = json.dumps(candidates, ensure_ascii=False)
            ptr = self._dll.execute_with_retry(
                candidates_json.encode("utf-8"),
                int(verify_delay_ms),
                int(max_attempts),
                1 if use_clipboard_check else 0,
                1 if use_window_check else 0,
            )
            if not ptr:
                return None
            raw = ctypes.cast(ptr, ctypes.c_char_p).value
            result = json.loads(raw.decode("utf-8")) if raw else None
            self._dll.free_result(ptr)
            return result
        except Exception:
            return None

    def send_hotkey(self, key_combination: str) -> bool:
        """发送热键组合。成功返回 True。"""
        if self._dll is None:
            return False
        try:
            return self._dll.send_hotkey(key_combination.encode("utf-8")) == 0
        except Exception:
            return False

    def validate_hotkey(self, key_combination: str) -> bool:
        """校验键位组合能否被原生层解析（不发送任何按键）。"""
        if self._dll is None:
            return False
        try:
            return self._dll.validate_hotkey(key_combination.encode("utf-8")) == 0
        except Exception:
            return False

    def send_unicode_char(self, text: str) -> bool:
        """发送 Unicode 文本。成功返回 True。"""
        if self._dll is None:
            return False
        try:
            return self._dll.send_unicode_char(text) == 0
        except Exception:
            return False

    def type_via_clipboard(self, text: str) -> bool:
        """通过剪贴板粘贴文本。成功返回 True。"""
        if self._dll is None:
            return False
        try:
            return self._dll.type_via_clipboard(text) == 0
        except Exception:
            return False

    def foreground_window(self) -> Optional[Dict[str, Any]]:
        """获取前台窗口信息。返回 dict 或 None。"""
        if self._dll is None:
            return None
        try:
            ptr = self._dll.foreground_window_json()
            if not ptr:
                return None
            # ptr 是 c_void_p（原始指针），ctypes 不会自动释放，需手动读 + 释放。
            raw = ctypes.cast(ptr, ctypes.c_char_p).value
            result = json.loads(raw.decode("utf-8")) if raw else None
            self._dll.free_result(ptr)
            return result
        except Exception:
            return None

    # ── fuzzy_match API ─────────────────────────────────────────────────

    def ac_build(self, keywords_json: str) -> Optional[int]:
        """构建 AC 自动机。keywords_json 是 JSON 数组字符串。
        返回不透明句柄（int），失败返回 None。调用方需用 ac_free 释放。"""
        if self._dll is None:
            return None
        try:
            h = self._dll.ac_build(keywords_json.encode("utf-8"))
            return int(h) if h else None
        except Exception:
            return None

    def ac_contains(self, handle: int, text: str) -> Optional[str]:
        """在 text 中查找任一已注册关键字；返回命中的 command 字符串或 None。"""
        if self._dll is None or not handle:
            return None
        try:
            ptr = self._dll.ac_contains(handle, text.encode("utf-8"))
            return self._consume_cstr(ptr)
        except Exception:
            return None

    def ac_free(self, handle: int) -> None:
        """释放 AC 自动机句柄。"""
        if self._dll is None or not handle:
            return
        try:
            self._dll.ac_free(handle)
        except Exception:
            pass

    def fuzzy_ratio(self, a: str, b: str) -> float:
        """计算两个字符串的相似度比率（0.0~1.0）。"""
        if self._dll is None:
            return 0.0
        try:
            return float(self._dll.fuzzy_ratio(a.encode("utf-8"), b.encode("utf-8")))
        except Exception:
            return 0.0

    def fuzzy_best_match(self, keywords_json: str, text: str,
                        threshold: float = 0.7) -> Optional[Dict[str, Any]]:
        """对一组候选关键字做最佳模糊匹配。返回 dict 或 None。"""
        if self._dll is None:
            return None
        try:
            ptr = self._dll.fuzzy_best_match(
                keywords_json.encode("utf-8"),
                text.encode("utf-8"),
                ctypes.c_double(threshold),
            )
            s = self._consume_cstr(ptr)
            return json.loads(s) if s else None
        except Exception:
            return None

    # ── scache API ──────────────────────────────────────────────────────

    def scache_create(self, max_entries: int = 500,
                     fuzzy_threshold: float = 0.6) -> Optional[int]:
        """创建语义缓存。返回不透明句柄或 None。调用方需用 scache_free 释放。"""
        if self._dll is None:
            return None
        try:
            h = self._dll.scache_create(max_entries, ctypes.c_double(fuzzy_threshold))
            return int(h) if h else None
        except Exception:
            return None

    def scache_free(self, handle: int) -> None:
        """释放语义缓存句柄。"""
        if self._dll is None or not handle:
            return
        try:
            self._dll.scache_free(handle)
        except Exception:
            pass

    def scache_set(self, handle: int, key: str, intent: str,
                  value_json: str, ttl_seconds: float = 300.0) -> bool:
        """添加一个条目。成功返回 True。"""
        if self._dll is None or not handle:
            return False
        try:
            return self._dll.scache_set(
                handle,
                key.encode("utf-8"),
                intent.encode("utf-8"),
                value_json.encode("utf-8"),
                ctypes.c_double(ttl_seconds),
            ) == 0
        except Exception:
            return False

    def scache_get(self, handle: int, key: str) -> Optional[Any]:
        """精确查找。返回反序列化的 JSON 值或 None。"""
        if self._dll is None or not handle:
            return None
        try:
            ptr = self._dll.scache_get(handle, key.encode("utf-8"))
            s = self._consume_cstr(ptr)
            return json.loads(s) if s else None
        except Exception:
            return None

    def scache_fuzzy_lookup(self, handle: int, intent: str) -> Optional[Any]:
        """模糊查找。返回反序列化的 JSON 值或 None。"""
        if self._dll is None or not handle:
            return None
        try:
            ptr = self._dll.scache_fuzzy_lookup(handle, intent.encode("utf-8"))
            s = self._consume_cstr(ptr)
            return json.loads(s) if s else None
        except Exception:
            return None

    def scache_clear(self, handle: int) -> None:
        """清空缓存。"""
        if self._dll is None or not handle:
            return
        try:
            self._dll.scache_clear(handle)
        except Exception:
            pass

    def scache_size(self, handle: int) -> int:
        """返回当前条目数。"""
        if self._dll is None or not handle:
            return 0
        try:
            return int(self._dll.scache_size(handle))
        except Exception:
            return 0

    # ── context_proc API ────────────────────────────────────────────────

    def get_process_name_by_pid(self, pid: int) -> Optional[str]:
        """根据 PID 获取进程可执行文件名。使用 QueryFullProcessImageNameW。"""
        if self._dll is None:
            return None
        try:
            ptr = self._dll.get_process_name_by_pid(ctypes.c_uint32(pid))
            return self._consume_cstr(ptr)
        except Exception:
            return None

    def get_foreground_context(self) -> Optional[Dict[str, Any]]:
        """一次性获取前台窗口的完整上下文（title/process_name/app_name/pid/hwnd）。
        内部用 QueryFullProcessImageNameW + 内置指纹表，<1ms。"""
        if self._dll is None:
            return None
        try:
            ptr = self._dll.get_foreground_context()
            if not ptr:
                return None
            raw = ctypes.cast(ptr, ctypes.c_char_p).value
            result = json.loads(raw.decode("utf-8")) if raw else None
            self._dll.free_result(ptr)
            return result
        except Exception:
            return None

    def fingerprint_process(self, process_name: str) -> Optional[str]:
        """将进程名映射为友好的应用名称。"""
        if self._dll is None:
            return None
        try:
            ptr = self._dll.fingerprint_process(process_name.encode("utf-8"))
            return self._consume_cstr(ptr)
        except Exception:
            return None

    # ── pattern_cluster API ─────────────────────────────────────────────

    def cluster_operations(self, records_json: str, gap_seconds: int = 30,
                          min_sequence_length: int = 1,
                          min_frequency: int = 3) -> Optional[list]:
        """对操作记录做模式聚类。返回 pattern 列表或 None。"""
        if self._dll is None:
            return None
        try:
            ptr = self._dll.cluster_operations(
                records_json.encode("utf-8"),
                gap_seconds, min_sequence_length, min_frequency,
            )
            s = self._consume_cstr(ptr)
            return json.loads(s) if s else None
        except Exception:
            return None

    def canonical_key(self, detail: str) -> Optional[str]:
        """将按键字符串规范化为唯一形式（与 Python operation_memory.canonical_key 等价）。"""
        if self._dll is None:
            return None
        try:
            ptr = self._dll.canonical_key(detail.encode("utf-8"))
            return self._consume_cstr(ptr)
        except Exception:
            return None


# ── 单例 ────────────────────────────────────────────────────────────

native = _NativeDLL()
