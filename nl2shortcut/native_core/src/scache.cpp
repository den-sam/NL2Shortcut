/// 语义缓存原生层 —— 倒排索引 + 位集 Jaccard 加速。
///
/// 设计要点
/// ────────
/// Python 版 SemanticCache._fuzzy_lookup 对每个缓存条目重新 _tokenize + 计算 Jaccard，
/// 500 条目时 ~25ms。本模块用以下两点加速：
///   1. 倒排索引：token -> 命中该 token 的条目集合
///   2. 位集（std::vector<uint64_t>）+ popcount：Jaccard 计算从 O(K) 降到 O(K/64)
///
/// 整体复杂度：O(K_query + N_候选 * K/64)，实测 500 条目可降至 < 1ms。
///
/// 分词算法与 Python 版 context_store._tokenize 完全一致：
///   - 提取连续中文 / 英文数字 token
///   - 中文做 1-gram + 2-gram 展开

#include "scache.h"
#include "inject.h"   // for free_result
#ifndef NOMINMAX
#  define NOMINMAX  // 抑制 Windows.h 的 min/max 宏，避免与 std::min/std::max 冲突
#endif
#include <Windows.h>
#include <cstring>
#include <string>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include <algorithm>
#include <cstdio>

// ═══════════════════════════════════════════════════════════════════════
// 中英文分词（与 Python context_store._tokenize 等价）
// ═══════════════════════════════════════════════════════════════════════

static bool is_cjk(char32_t cp) {
    return cp >= 0x4E00 && cp <= 0x9FFF;
}

static bool is_ascii_word_char(char c) {
    return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
}

/// 解码一个 UTF-8 字符，返回 codepoint 并推进 p。
/// 失败返回 U+FFFD。
static char32_t utf8_decode(const char*& p, const char* end) {
    if (p >= end) return 0;
    unsigned char c = (unsigned char)*p;
    if (c < 0x80) { p++; return c; }
    if ((c & 0xE0) == 0xC0) {
        if (p + 1 >= end) { p++; return 0xFFFD; }
        char32_t cp = ((char32_t)(c & 0x1F) << 6) | ((char32_t)(unsigned char)p[1] & 0x3F);
        p += 2;
        return cp;
    }
    if ((c & 0xF0) == 0xE0) {
        if (p + 2 >= end) { p++; return 0xFFFD; }
        char32_t cp = ((char32_t)(c & 0x0F) << 12)
                    | ((char32_t)(unsigned char)p[1] & 0x3F) << 6
                    | ((char32_t)(unsigned char)p[2] & 0x3F);
        p += 3;
        return cp;
    }
    if ((c & 0xF8) == 0xF0) {
        if (p + 3 >= end) { p++; return 0xFFFD; }
        char32_t cp = ((char32_t)(c & 0x07) << 18)
                    | ((char32_t)(unsigned char)p[1] & 0x3F) << 12
                    | ((char32_t)(unsigned char)p[2] & 0x3F) << 6
                    | ((char32_t)(unsigned char)p[3] & 0x3F);
        p += 4;
        return cp;
    }
    p++;
    return 0xFFFD;
}

/// 将 codepoint 编码为 UTF-8（用于 2-gram 子串）。
static std::string utf8_encode(char32_t cp) {
    std::string s;
    if (cp < 0x80) {
        s.push_back((char)cp);
    } else if (cp < 0x800) {
        s.push_back((char)(0xC0 | (cp >> 6)));
        s.push_back((char)(0x80 | (cp & 0x3F)));
    } else {
        s.push_back((char)(0xE0 | (cp >> 12)));
        s.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        s.push_back((char)(0x80 | (cp & 0x3F)));
    }
    return s;
}

static std::vector<std::string> tokenize(const std::string& text) {
    std::vector<std::string> tokens;
    std::string lower;
    lower.reserve(text.size());
    for (char c : text) {
        lower.push_back((char)(unsigned char)tolower((unsigned char)c));
    }

    const char* p = lower.c_str();
    const char* end = p + lower.size();

    while (p < end) {
        // 跳过分隔符
        while (p < end) {
            unsigned char c = (unsigned char)*p;
            if (c < 0x80) {
                if (is_ascii_word_char((char)c)) break;
                // 可能是 CJK 起点
                p++;
            } else {
                break;
            }
        }
        if (p >= end) break;

        // 检测是否为 CJK 字符
        const char* tok_start = p;
        char32_t cp = utf8_decode(p, end);
        if (is_cjk(cp)) {
            // 收集连续 CJK
            std::vector<char32_t> cps;
            cps.push_back(cp);
            while (p < end) {
                const char* save = p;
                char32_t next = utf8_decode(p, end);
                if (is_cjk(next)) {
                    cps.push_back(next);
                } else {
                    p = save;
                    break;
                }
            }
            // 添加完整词
            std::string full;
            for (char32_t c : cps) full += utf8_encode(c);
            tokens.push_back(full);
            // 1-gram
            for (char32_t c : cps) tokens.push_back(utf8_encode(c));
            // 2-gram
            for (size_t i = 0; i + 1 < cps.size(); i++) {
                tokens.push_back(utf8_encode(cps[i]) + utf8_encode(cps[i+1]));
            }
        } else if (cp < 0x80 && is_ascii_word_char((char)cp)) {
            // ASCII 词
            std::string word;
            word.push_back((char)cp);
            while (p < end) {
                unsigned char c = (unsigned char)*p;
                if (c < 0x80 && is_ascii_word_char((char)c)) {
                    word.push_back((char)c);
                    p++;
                } else break;
            }
            tokens.push_back(word);
        } else {
            // 其他字符跳过
        }
    }
    return tokens;
}

// ═══════════════════════════════════════════════════════════════════════
// 位集容器（基于 uint64_t 数组）
// ═══════════════════════════════════════════════════════════════════════

struct BitSet {
    std::vector<uint64_t> words;
    size_t bits;

    BitSet() : bits(0) {}

    void resize(size_t n) {
        bits = n;
        size_t need = (n + 63) / 64;
        // 必须用 std::vector::resize（保留旧元素），不能用 assign（会清空旧位）
        if (words.size() < need) {
            words.resize(need, 0);
        }
        // 缩小时不删 words，仅缩小逻辑 bits 上限；多余位不会被访问（set/test 都检查 i<bits）
    }

    void set(size_t i) {
        if (i >= bits) return;
        words[i / 64] |= (1ULL << (i % 64));
    }

    bool test(size_t i) const {
        if (i >= bits) return false;
        return (words[i / 64] >> (i % 64)) & 1ULL;
    }

    size_t popcount() const {
        size_t cnt = 0;
        for (uint64_t w : words) {
            cnt += __popcnt64(w);
        }
        return cnt;
    }
};

// 与（AND）— 求交集大小
static size_t bitset_and_count(const BitSet& a, const BitSet& b) {
    size_t cnt = 0;
    size_t n = std::min(a.words.size(), b.words.size());
    for (size_t i = 0; i < n; i++) {
        cnt += __popcnt64(a.words[i] & b.words[i]);
    }
    return cnt;
}

// 或（OR）— 求并集大小
static size_t bitset_or_count(const BitSet& a, const BitSet& b) {
    size_t cnt = 0;
    size_t n = std::max(a.words.size(), b.words.size());
    for (size_t i = 0; i < n; i++) {
        uint64_t aw = (i < a.words.size()) ? a.words[i] : 0;
        uint64_t bw = (i < b.words.size()) ? b.words[i] : 0;
        cnt += __popcnt64(aw | bw);
    }
    return cnt;
}

// ═══════════════════════════════════════════════════════════════════════
// 缓存条目
// ═══════════════════════════════════════════════════════════════════════

struct CacheEntry {
    std::string key;
    std::string intent;
    std::string value_json;
    double expiry_ts;     // 绝对过期时间
    BitSet token_bitmap;  // 在 token_dict 中的位图
    size_t token_count;   // 该条目的去重 token 数
};

struct SemanticCacheNative {
    int max_entries;
    double fuzzy_threshold;

    // 条目存储：key -> entry index
    std::vector<CacheEntry> entries;
    std::unordered_map<std::string, size_t> key_to_idx;

    // 倒排索引：token -> 命中该 token 的条目 index 集合
    std::unordered_map<std::string, std::vector<size_t>> inverted_index;

    // token 字典：token -> 全局 token id
    std::unordered_map<std::string, size_t> token_dict;

    SemanticCacheNative(int max_ent, double thr)
        : max_entries(max_ent), fuzzy_threshold(thr) {}

    /// 给一个条目构建位图（分配 token id）
    BitSet build_bitmap(const std::vector<std::string>& tokens) {
        std::unordered_set<std::string> unique_tokens(tokens.begin(), tokens.end());
        BitSet bs;
        bs.resize(token_dict.size() + unique_tokens.size());
        for (const auto& t : unique_tokens) {
            auto it = token_dict.find(t);
            size_t id;
            if (it == token_dict.end()) {
                id = token_dict.size();
                token_dict[t] = id;
                // 扩展所有现有位图的大小（懒扩展：访问时检查）
                bs.resize(token_dict.size() + unique_tokens.size());
            } else {
                id = it->second;
            }
            bs.set(id);
        }
        return bs;
    }

    /// 删除条目（同时清理倒排索引）
    void erase_entry(size_t idx) {
        CacheEntry& e = entries[idx];
        // 从倒排索引移除
        std::vector<std::string> tokens = tokenize(e.intent);
        std::unordered_set<std::string> unique_tokens(tokens.begin(), tokens.end());
        for (const auto& t : unique_tokens) {
            auto it = inverted_index.find(t);
            if (it != inverted_index.end()) {
                auto& vec = it->second;
                vec.erase(std::remove(vec.begin(), vec.end(), idx), vec.end());
                if (vec.empty()) inverted_index.erase(it);
            }
        }
        key_to_idx.erase(e.key);
        // 标记为空（用空 key 占位，避免索引重排）
        e.key.clear();
        e.intent.clear();
        e.value_json.clear();
    }

    /// 清理过期条目
    void evict_expired(double now) {
        for (size_t i = 0; i < entries.size(); i++) {
            if (!entries[i].key.empty() && entries[i].expiry_ts <= now) {
                erase_entry(i);
            }
        }
    }
};

// ═══════════════════════════════════════════════════════════════════════
// 导出 API
// ═══════════════════════════════════════════════════════════════════════

SemanticCacheNative* scache_create(int max_entries, double fuzzy_threshold) {
    if (max_entries <= 0) max_entries = 500;
    if (fuzzy_threshold <= 0) fuzzy_threshold = 0.6;
    return new SemanticCacheNative(max_entries, fuzzy_threshold);
}

void scache_free(SemanticCacheNative* sc) {
    delete sc;
}

int scache_set(
    SemanticCacheNative* sc,
    const char* key,
    const char* intent,
    const char* value_json,
    double ttl_seconds
) {
    if (!sc || !key || !intent || !value_json) return -1;

    double now = (double)GetTickCount64() / 1000.0;

    // 如果已存在，先删除旧条目
    auto it = sc->key_to_idx.find(key);
    if (it != sc->key_to_idx.end()) {
        sc->erase_entry(it->second);
    }

    // LRU：超过上限时删一个最旧的
    if ((int)sc->key_to_idx.size() >= sc->max_entries) {
        // 找到 expiry_ts 最小的非空条目
        double min_exp = 1e18;
        size_t min_idx = (size_t)-1;
        for (size_t i = 0; i < sc->entries.size(); i++) {
            if (!sc->entries[i].key.empty() && sc->entries[i].expiry_ts < min_exp) {
                min_exp = sc->entries[i].expiry_ts;
                min_idx = i;
            }
        }
        if (min_idx != (size_t)-1) {
            sc->erase_entry(min_idx);
        }
    }

    // 找一个空槽或追加
    size_t idx;
    bool found_empty = false;
    for (size_t i = 0; i < sc->entries.size(); i++) {
        if (sc->entries[i].key.empty()) {
            idx = i;
            found_empty = true;
            break;
        }
    }
    if (!found_empty) {
        idx = sc->entries.size();
        sc->entries.emplace_back();
    }

    CacheEntry& e = sc->entries[idx];
    e.key = key;
    e.intent = intent;
    e.value_json = value_json;
    e.expiry_ts = now + ttl_seconds;

    // 构建位图
    std::vector<std::string> tokens = tokenize(intent);
    e.token_count = std::unordered_set<std::string>(tokens.begin(), tokens.end()).size();
    e.token_bitmap = sc->build_bitmap(tokens);

    // 更新倒排索引
    std::unordered_set<std::string> unique_tokens(tokens.begin(), tokens.end());
    for (const auto& t : unique_tokens) {
        sc->inverted_index[t].push_back(idx);
    }

    sc->key_to_idx[key] = idx;
    return 0;
}

const char* scache_get(SemanticCacheNative* sc, const char* key) {
    if (!sc || !key) return nullptr;
    double now = (double)GetTickCount64() / 1000.0;

    auto it = sc->key_to_idx.find(key);
    if (it == sc->key_to_idx.end()) return nullptr;

    CacheEntry& e = sc->entries[it->second];
    if (e.expiry_ts <= now) {
        sc->erase_entry(it->second);
        return nullptr;
    }

    // 复制 value_json 给调用方
    size_t len = e.value_json.size() + 1;
    char* buf = (char*)CoTaskMemAlloc(len);
    if (buf) memcpy(buf, e.value_json.c_str(), len);
    return buf;
}

const char* scache_fuzzy_lookup(SemanticCacheNative* sc, const char* intent) {
    if (!sc || !intent) return nullptr;
    double now = (double)GetTickCount64() / 1000.0;

    std::vector<std::string> query_tokens = tokenize(intent);
    if (query_tokens.empty()) return nullptr;

    std::unordered_set<std::string> unique_query(query_tokens.begin(), query_tokens.end());

    // 候选集：所有命中过任一 token 的条目
    std::unordered_set<size_t> candidates;
    for (const auto& t : unique_query) {
        auto it = sc->inverted_index.find(t);
        if (it != sc->inverted_index.end()) {
            for (size_t idx : it->second) {
                if (!sc->entries[idx].key.empty() && sc->entries[idx].expiry_ts > now) {
                    candidates.insert(idx);
                }
            }
        }
    }

    if (candidates.empty()) return nullptr;

    // 构建查询位图
    BitSet query_bitmap;
    query_bitmap.resize(sc->token_dict.size());
    for (const auto& t : unique_query) {
        auto it = sc->token_dict.find(t);
        if (it != sc->token_dict.end()) {
            query_bitmap.set(it->second);
        }
    }

    // 遍历候选，计算 Jaccard = |A ∩ B| / |A ∪ B|
    double best_score = 0.0;
    size_t best_idx = (size_t)-1;

    for (size_t idx : candidates) {
        CacheEntry& e = sc->entries[idx];
        // 调整位图大小（懒扩展）
        if (e.token_bitmap.bits < query_bitmap.bits) {
            e.token_bitmap.resize(query_bitmap.bits);
        }
        size_t inter = bitset_and_count(query_bitmap, e.token_bitmap);
        if (inter == 0) continue;
        // |union| = |a| + |b| - |inter|
        // 但因为位图可能大小不一致，用并集大小更准
        size_t uni = bitset_or_count(query_bitmap, e.token_bitmap);
        if (uni == 0) continue;
        double score = (double)inter / (double)uni;
        if (score >= sc->fuzzy_threshold && score > best_score) {
            best_score = score;
            best_idx = idx;
        }
    }

    if (best_idx == (size_t)-1) return nullptr;

    CacheEntry& e = sc->entries[best_idx];
    size_t len = e.value_json.size() + 1;
    char* buf = (char*)CoTaskMemAlloc(len);
    if (buf) memcpy(buf, e.value_json.c_str(), len);
    return buf;
}

void scache_clear(SemanticCacheNative* sc) {
    if (!sc) return;
    sc->entries.clear();
    sc->key_to_idx.clear();
    sc->inverted_index.clear();
    sc->token_dict.clear();
}

int scache_size(SemanticCacheNative* sc) {
    if (!sc) return 0;
    return (int)sc->key_to_idx.size();
}
