#pragma once

#include "export.h"

#ifdef __cplusplus
extern "C" {
#endif

/// 获取前台窗口信息，返回 JSON 字符串。
/// 调用方需用 free_result() 释放。
/// @return JSON: {title, process_name, process_id, hwnd, platform}
NL2S_API const char* foreground_window_json();

#ifdef __cplusplus
}
#endif
