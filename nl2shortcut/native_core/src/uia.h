#pragma once

#include "export.h"

#ifdef __cplusplus
extern "C" {
#endif

/// 采集 UIA 树快照并序列化为 JSON 字符串。
/// 调用方需用 free_result() 释放返回的字符串。
/// @param max_depth  最大递归深度 (默认 12)
/// @param max_nodes  最大节点数 (默认 500)
/// @return JSON 字符串 (CoTaskMemAlloc 分配), 失败返回 nullptr
NL2S_API const char* uia_snapshot(int max_depth, int max_nodes);

/// 对比两棵 UIA 树 JSON 快照，输出节点级 diff（不过滤任何差异）。
///
/// 输入：before_json / after_json 为 uia_snapshot() 返回的 JSON 字符串。
/// 输出：JSON 字符串，格式：
///   {
///     "changed": [ {"path":"...","field":"name","before":"...","after":"..."} ],
///     "added":    [ {"path":"...","node":{...}} ],
///     "removed":  [ {"path":"...","node":{...}} ],
///     "summary":  {"changed":N,"added":N,"removed":N,"total_before":N,"total_after":N}
///   }
/// 节点匹配键：以 name+control_type+role 为复合键；若键冲突则用 children 索引兜底。
/// 对比字段：name / value / state / x / y / width / height / patterns / keyboard_shortcut。
/// @return JSON 字符串 (CoTaskMemAlloc 分配), 失败返回 nullptr
NL2S_API const char* uia_diff(const char* before_json, const char* after_json);

/// ── 过滤标记位（uia_diff_filtered 的 filter_flags）──
///   0x01 UIA_FILTER_FOCUS_STATE  : 忽略 state 字段中 focused/focusable 相关变化
///   0x02 UIA_FILTER_POSITION_PX  : 忽略位置/尺寸变化 < position_tolerance 像素（默认 10px）
///   0x04 UIA_FILTER_META_FIELDS  : 忽略 elapsed_ms / 快照元信息字段的差异
///   0xFF UIA_FILTER_ALL          : 启用上述全部非关键过滤（推荐默认）
enum {
    UIA_FILTER_FOCUS_STATE = 0x01,
    UIA_FILTER_POSITION_PX = 0x02,
    UIA_FILTER_META_FIELDS = 0x04,
    UIA_FILTER_ALL         = 0xFF,
};

/// 带过滤器的 UIA diff（推荐用于真实窗口对比，自动过滤焦点/位置抖动等非关键差异）。
///
/// @param filter_flags         过滤位掩码，见上方 enum（传 0 = 不过滤，等同 uia_diff）
/// @param position_tolerance_px 位置/尺寸容忍像素阈值（默认 10，仅在 UIA_FILTER_POSITION_PX 启用时生效）
NL2S_API const char* uia_diff_filtered(
    const char* before_json,
    const char* after_json,
    int filter_flags,
    int position_tolerance_px
);

#ifdef __cplusplus
}
#endif
