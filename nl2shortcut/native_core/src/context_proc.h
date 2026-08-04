#pragma once

#include "export.h"

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/// 根据 PID 获取进程可执行文件名（不含路径，小写）。
/// 使用 QueryFullProcessImageNameW —— 比 CreateToolhelp32Snapshot 快 50-100x，
/// 无需遍历进程列表，且只请求 PROCESS_QUERY_LIMITED_INFORMATION（无需管理员权限）。
///
/// @param pid 进程 ID。
/// @return 进程名（如 "chrome.exe"），调用方需用 free_result() 释放。
///         失败返回 nullptr。
NL2S_API const char* get_process_name_by_pid(uint32_t pid);

/// 获取前台窗口的完整上下文，返回 JSON：
///   {"title":"...", "process_name":"chrome.exe", "process_id":1234,
///    "app_name":"chrome", "hwnd":12345678, "platform":"windows"}
///
/// 内部一次性完成：取前台 HWND → 取标题 → 取 PID → QueryFullProcessImageNameW
/// → 通过内置指纹表把 process_name 映射为友好 app_name（如 "chrome.exe" → "chrome"）。
///
/// @return JSON 字符串指针，调用方需用 free_result() 释放。失败返回 nullptr。
NL2S_API const char* get_foreground_context();

/// 将进程名映射为友好的应用名称（内置指纹表）。
/// 例如 "chrome.exe" → "chrome"，"Code.exe" → "vscode"。
///
/// @param process_name 进程名（含或不含 .exe 后缀均可）。
/// @return 友好名称（C 字符串，调用方需用 free_result() 释放）。
NL2S_API const char* fingerprint_process(const char* process_name);

#ifdef __cplusplus
}
#endif
