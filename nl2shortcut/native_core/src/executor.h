#pragma once

#include "export.h"

#ifdef __cplusplus
extern "C" {
#endif

/// C++ 执行内核：把"候选键构建 + 执行循环 + 验证 + 重试"整体下沉到 C++。
///
/// 输入：
///   candidates_json : JSON 数组字符串，如 ["Ctrl+C","Ctrl+Insert"]
///   verify_delay_ms : 每次注入后的验证等待时间（毫秒）
///   max_attempts    : 最大重试次数（含首次，默认 3）
///   use_clipboard_check : 是否启用剪贴板验证（1=是, 0=否）
///   use_window_check    : 是否启用前台窗口验证（1=是, 0=否）
///
/// 输出（JSON 字符串，CoTaskMemAlloc 分配）：
///   {
///     "success": true/false,
///     "used_key": "Ctrl+C",
///     "attempts": 2,
///     "error": null,           // 失败原因（字符串或 null）
///     "elapsed_ms": 12.3,
///     "verifications": [        // 每次尝试的验证详情
///       {"key":"Ctrl+C","attempt":1,"verified":false,"reason":"clipboard unchanged"},
///       {"key":"Ctrl+Insert","attempt":2,"verified":true,"reason":"ok"}
///     ]
///   }
///
/// 验证机制：
///   - clipboard：注入前读取剪贴板文本，注入后再次读取，对比是否变化
///   - window：注入前读取前台窗口标题，注入后再次读取，对比是否变化
///   - 任意一个验证维度变化即视为成功
///   - 若 use_clipboard_check=0 且 use_window_check=0，则不验证，首次注入即成功
///
/// @return JSON 字符串; 失败返回 nullptr
NL2S_API const char* execute_with_retry(
    const char* candidates_json,
    int verify_delay_ms,
    int max_attempts,
    int use_clipboard_check,
    int use_window_check
);

#ifdef __cplusplus
}
#endif
