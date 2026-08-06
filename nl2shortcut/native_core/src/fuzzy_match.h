#pragma once

#include "export.h"

#ifdef __cplusplus
extern "C" {
#endif

/// 构建一个 AC 自动机匹配器，用于在意图文本中查找关键字子串。
/// 调用方传入 JSON 数组：[{"keyword":"copy","command":"copy"}, ...]
/// 返回一个不透明句柄；失败返回 nullptr。
NL2S_API void* ac_build(const char* keywords_json);

/// 在 text 中查找任一已注册关键字子串；返回首个命中的 command 字符串。
/// 未命中返回 nullptr（调用方需自行 free_result）。
/// 返回的字符串是 command 字段，长度不超过 256。
NL2S_API const char* ac_contains(void* handle, const char* text);

/// 释放 AC 自动机句柄。
NL2S_API void ac_free(void* handle);

/// 计算两个字符串的相似度比率（0.0~1.0），与 difflib.SequenceMatcher.ratio() 等价。
/// 用于模糊匹配回退路径。
/// 返回值范围 [0.0, 1.0]；任一为空返回 0.0。
NL2S_API double fuzzy_ratio(const char* a, const char* b);

/// 对一组候选关键字做最佳模糊匹配。
/// keywords_json: [{"keyword":"copy","command":"copy"}, ...]
/// text: 待匹配文本（已分词后空格连接的字符串）
/// threshold: 最小相似度阈值（如 0.7）
/// 返回 JSON：{"command":"copy","score":0.85,"keyword":"copy"} 或 nullptr
/// 调用方需 free_result 释放返回的字符串。
NL2S_API const char* fuzzy_best_match(
    const char* keywords_json,
    const char* text,
    double threshold
);

#ifdef __cplusplus
}
#endif
