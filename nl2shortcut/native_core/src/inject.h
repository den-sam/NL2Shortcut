#pragma once

#include "export.h"

#ifdef __cplusplus
extern "C" {
#endif

/// 发送热键组合 (如 "Ctrl+C", "Alt+Tab")
/// @return 0 成功, -1 失败
NL2S_API int send_hotkey(const char* key_combination);

/// 发送 Unicode 文本 (KEYEVENTF_UNICODE 方式)
/// @return 0 成功, -1 失败
NL2S_API int send_unicode_char(const wchar_t* text);

/// 通过剪贴板粘贴文本 (Ctrl+V 方式)
/// @return 0 成功, -1 失败
NL2S_API int type_via_clipboard(const wchar_t* text);

/// 校验键位组合能否被解析（**不发送任何按键**）。
/// 供上层做全库自检，避免为了验证解析而真的按下 177 组快捷键。
/// @return 0 可解析, -1 不可解析
NL2S_API int validate_hotkey(const char* key_combination);

/// 释放 native 返回的字符串内存
NL2S_API void free_result(void* ptr);

#ifdef __cplusplus
}
#endif
