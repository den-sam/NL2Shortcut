#pragma once

#include "export.h"

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/// 语义缓存句柄。内部维护 token → 条目位图 的倒排索引，
/// 让 Jaccard 查找从 O(N*K) 降到 O(K + N_候选)。
typedef struct SemanticCacheNative SemanticCacheNative;

/// 创建一个空缓存。
/// max_entries: 最大条目数（默认 500）。
/// fuzzy_threshold: Jaccard 相似度阈值（默认 0.6）。
NL2S_API SemanticCacheNative* scache_create(int max_entries, double fuzzy_threshold);

/// 释放缓存句柄。
NL2S_API void scache_free(SemanticCacheNative* sc);

/// 添加一个条目。
/// key: SHA256 前 16 位的 hash key（C 字符串）
/// intent: 原始意图文本（用于分词建索引）
/// value_json: 缓存值（任意 JSON 字符串）
/// ttl_seconds: 生存时间（秒）
/// 返回 0 成功，-1 失败。
NL2S_API int scache_set(
    SemanticCacheNative* sc,
    const char* key,
    const char* intent,
    const char* value_json,
    double ttl_seconds
);

/// 精确查找（按 key 命中）。
/// 命中时返回 value_json 的指针（调用方需 free_result），未命中返回 nullptr。
/// 过期条目会被自动删除。
NL2S_API const char* scache_get(SemanticCacheNative* sc, const char* key);

/// 模糊查找（Jaccard）。
/// intent: 待查询的意图文本。
/// 返回命中条目的 value_json 指针（调用方需 free_result），未命中返回 nullptr。
NL2S_API const char* scache_fuzzy_lookup(SemanticCacheNative* sc, const char* intent);

/// 清空所有条目。
NL2S_API void scache_clear(SemanticCacheNative* sc);

/// 返回当前条目数。
NL2S_API int scache_size(SemanticCacheNative* sc);

#ifdef __cplusplus
}
#endif
