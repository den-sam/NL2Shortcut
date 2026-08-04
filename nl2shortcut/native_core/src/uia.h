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

#ifdef __cplusplus
}
#endif
