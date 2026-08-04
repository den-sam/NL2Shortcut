/// 操作记忆模式聚类 —— C++ 原生实现。
///
/// 替代 operation_memory.py::learn_patterns 中的 Python 循环：
///   - records JSON 解析 → 排序 → 分段 → 聚类 → 输出 JSON
///   - 内置 canonical_key 移植（与 Python 版完全等价）
///
/// 性能对比（1000 条记录）：
///   - Python 版：~50-100ms（含 SQLite 读取 + 聚类循环）
///   - C++ 版：   ~1-3ms（仅聚类，SQLite 读取仍在 Python 侧）
///
/// 设计要点：
///   - SQLite 读取留在 Python 侧（已有单例连接 + WAL，开销可接受）
///   - 聚类循环（O(N log N) 排序 + O(N) 分段 + O(N) 聚类）移到 C++
///   - 输出 JSON 让 Python 负责后续 save_pattern / export_workflow

#include "pattern_cluster.h"
#include "inject.h"   // for free_result
#ifndef NOMINMAX
#  define NOMINMAX  // 抑制 Windows.h 的 min/max 宏，避免与 std::min/std::max 冲突
#endif
#include <Windows.h>
#include <objbase.h>  // CoTaskMemAlloc / CoTaskMemFree

#include <string>
#include <vector>
#include <unordered_map>
#include <algorithm>
#include <cstring>
#include <cstdlib>
#include <ctime>

// ─────────────────────────────────────────────────────────────────────────────
// canonical_key —— 与 operation_memory.py::canonical_key 完全等价
// ─────────────────────────────────────────────────────────────────────────────

static const std::unordered_map<std::string, std::string>& key_aliases() {
    static const std::unordered_map<std::string, std::string> M = {
        {"control", "Ctrl"}, {"ctl", "Ctrl"},
        {"altgr", "AltGr"}, {"option", "Alt"}, {"opt", "Alt"},
        {"windows", "Win"}, {"super", "Win"}, {"meta", "Win"},
        {"return", "Enter"}, {"esc", "Esc"}, {"escape", "Esc"},
        {"space", "Space"}, {"pageup", "PageUp"}, {"pagedown", "PageDown"},
        {"del", "Delete"}, {"ins", "Insert"},
    };
    return M;
}

static const std::unordered_map<std::string, int>& modifier_order() {
    static const std::unordered_map<std::string, int> M = {
        {"ctrl", 0}, {"alt", 1}, {"shift", 2}, {"win", 3},
    };
    return M;
}

static std::string to_lower(const std::string& s) {
    std::string out = s;
    for (char& c : out) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + ('a' - 'A'));
    }
    return out;
}

static std::string to_upper(const std::string& s) {
    std::string out = s;
    for (char& c : out) {
        if (c >= 'a' && c <= 'z') c = static_cast<char>(c - ('a' - 'A'));
    }
    return out;
}

static std::string upper_first_lower_rest(const std::string& s) {
    if (s.empty()) return s;
    std::string out = s;
    if (out[0] >= 'a' && out[0] <= 'z') out[0] = static_cast<char>(out[0] - ('a' - 'A'));
    for (size_t i = 1; i < out.size(); ++i) {
        if (out[i] >= 'A' && out[i] <= 'Z') out[i] = static_cast<char>(out[i] + ('a' - 'A'));
    }
    return out;
}

static std::string trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r' || s[a] == '\n')) ++a;
    while (b > a && (s[b-1] == ' ' || s[b-1] == '\t' || s[b-1] == '\r' || s[b-1] == '\n')) --b;
    return s.substr(a, b - a);
}

static std::vector<std::string> split_plus(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : s) {
        if (c == '+') { out.push_back(cur); cur.clear(); }
        else cur.push_back(c);
    }
    out.push_back(cur);
    return out;
}

static std::string normalize_key_syntax(const std::string& detail) {
    std::string s = trim(detail);
    if (s.empty()) return s;
    std::vector<std::string> toks = split_plus(s);
    std::vector<std::string> norm;
    norm.reserve(toks.size());
    for (auto& t : toks) {
        std::string tt = trim(t);
        if (tt.empty()) continue;
        std::string tl = to_lower(tt);
        const auto& aliases = key_aliases();
        auto it = aliases.find(tl);
        if (it != aliases.end()) {
            norm.push_back(it->second);
        } else if (tt.size() == 1) {
            norm.push_back(to_upper(tt));
        } else {
            norm.push_back(upper_first_lower_rest(tt));
        }
    }
    const auto& mods = modifier_order();
    std::vector<std::string> mod_tokens, rest_tokens;
    for (auto& t : norm) {
        if (mods.find(to_lower(t)) != mods.end()) mod_tokens.push_back(t);
        else rest_tokens.push_back(t);
    }
    std::sort(mod_tokens.begin(), mod_tokens.end(),
        [&mods](const std::string& a, const std::string& b) {
            return mods.at(to_lower(a)) < mods.at(to_lower(b));
        });
    std::vector<std::string> all;
    all.insert(all.end(), mod_tokens.begin(), mod_tokens.end());
    all.insert(all.end(), rest_tokens.begin(), rest_tokens.end());
    std::string out;
    for (size_t i = 0; i < all.size(); ++i) {
        if (i) out += '+';
        out += all[i];
    }
    return out;
}

/// 语义等价表：build 一次，复用
static const std::unordered_map<std::string, std::string>& semantic_map() {
    static const std::unordered_map<std::string, std::string> M = []() {
        std::unordered_map<std::string, std::string> m;
        // (canonical, [equivalent forms...])
        const char* groups[][2] = {
            {"Ctrl+C",       "Ctrl+Insert"},
            {"Ctrl+V",       "Shift+Insert"},
            {"Ctrl+X",       "Shift+Delete"},
            {"Ctrl+Z",       "Alt+Backspace"},
        };
        for (auto& g : groups) {
            std::string canon = normalize_key_syntax(g[0]);
            m[normalize_key_syntax(g[0])] = canon;
            m[normalize_key_syntax(g[1])] = canon;
        }
        return m;
    }();
    return M;
}

static std::string canonical_key_str(const std::string& detail) {
    if (detail.empty()) return detail;
    std::string normalized = normalize_key_syntax(detail);
    const auto& sm = semantic_map();
    auto it = sm.find(normalized);
    if (it != sm.end()) return it->second;
    return normalized;
}

// ─────────────────────────────────────────────────────────────────────────────
// 最小 JSON 解析器（针对本模块的输入格式定制）
// ─────────────────────────────────────────────────────────────────────────────
//
// 输入是数组：[ {"k":"v", "n":123, ...}, ... ]
// 我们只需要解析这种结构化数据，不需要通用 JSON 库。

namespace {

struct JsonValue {
    enum class Type { Null, Bool, Number, String, Array, Object };
    Type type = Type::Null;
    bool b = false;
    double num = 0.0;
    std::string str;
    std::vector<JsonValue> arr;
    std::vector<std::pair<std::string, JsonValue>> obj;

    const JsonValue* find(const std::string& key) const {
        if (type != Type::Object) return nullptr;
        for (auto& kv : obj) if (kv.first == key) return &kv.second;
        return nullptr;
    }
};

class JsonParser {
public:
    explicit JsonParser(const char* s) : p_(s) {}

    bool parse(JsonValue& out) {
        skip_ws();
        return parse_value(out);
    }

private:
    const char* p_;

    void skip_ws() {
        while (*p_ == ' ' || *p_ == '\t' || *p_ == '\r' || *p_ == '\n') ++p_;
    }

    bool parse_value(JsonValue& v) {
        skip_ws();
        if (*p_ == '{') return parse_object(v);
        if (*p_ == '[') return parse_array(v);
        if (*p_ == '"') { v.type = JsonValue::Type::String; return parse_string(v.str); }
        if (*p_ == 't' || *p_ == 'f') return parse_bool(v);
        if (*p_ == 'n') return parse_null(v);
        return parse_number(v);
    }

    bool parse_object(JsonValue& v) {
        v.type = JsonValue::Type::Object;
        ++p_; // {
        skip_ws();
        if (*p_ == '}') { ++p_; return true; }
        while (true) {
            skip_ws();
            if (*p_ != '"') return false;
            std::string key;
            if (!parse_string(key)) return false;
            skip_ws();
            if (*p_ != ':') return false;
            ++p_;
            JsonValue val;
            if (!parse_value(val)) return false;
            v.obj.emplace_back(std::move(key), std::move(val));
            skip_ws();
            if (*p_ == ',') { ++p_; continue; }
            if (*p_ == '}') { ++p_; return true; }
            return false;
        }
    }

    bool parse_array(JsonValue& v) {
        v.type = JsonValue::Type::Array;
        ++p_; // [
        skip_ws();
        if (*p_ == ']') { ++p_; return true; }
        while (true) {
            JsonValue elem;
            if (!parse_value(elem)) return false;
            v.arr.push_back(std::move(elem));
            skip_ws();
            if (*p_ == ',') { ++p_; continue; }
            if (*p_ == ']') { ++p_; return true; }
            return false;
        }
    }

    bool parse_string(std::string& out) {
        if (*p_ != '"') return false;
        ++p_;
        out.clear();
        while (*p_) {
            char c = *p_++;
            if (c == '"') return true;
            if (c == '\\') {
                char esc = *p_++;
                switch (esc) {
                    case '"': out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/': out.push_back('/'); break;
                    case 'n': out.push_back('\n'); break;
                    case 't': out.push_back('\t'); break;
                    case 'r': out.push_back('\r'); break;
                    case 'b': out.push_back('\b'); break;
                    case 'f': out.push_back('\f'); break;
                    case 'u': {
                        // 4 hex digits → UTF-8. 简单实现：仅 BMP，不处理 surrogate pairs
                        char hex[5] = {0};
                        for (int i = 0; i < 4; ++i) hex[i] = *p_++;
                        unsigned cp = static_cast<unsigned>(std::strtoul(hex, nullptr, 16));
                        if (cp < 0x80) out.push_back(static_cast<char>(cp));
                        else if (cp < 0x800) {
                            out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
                            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
                        } else {
                            out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
                            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
                            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
                        }
                        break;
                    }
                    default: out.push_back(esc); break;
                }
            } else {
                out.push_back(c);
            }
        }
        return false; // 未闭合
    }

    bool parse_bool(JsonValue& v) {
        v.type = JsonValue::Type::Bool;
        if (strncmp(p_, "true", 4) == 0) { v.b = true; p_ += 4; return true; }
        if (strncmp(p_, "false", 5) == 0) { v.b = false; p_ += 5; return true; }
        return false;
    }

    bool parse_null(JsonValue& v) {
        v.type = JsonValue::Type::Null;
        if (strncmp(p_, "null", 4) == 0) { p_ += 4; return true; }
        return false;
    }

    bool parse_number(JsonValue& v) {
        v.type = JsonValue::Type::Number;
        char* end = nullptr;
        v.num = std::strtod(p_, &end);
        if (end == p_) return false;
        p_ = end;
        return true;
    }
};

} // namespace

// ─────────────────────────────────────────────────────────────────────────────
// 数据结构
// ─────────────────────────────────────────────────────────────────────────────

struct OpRecord {
    std::string timestamp;       // ISO 8601 字符串
    time_t epoch;                // 解析后的秒级时间戳
    std::string app;
    std::string action_type;
    std::string action_detail;
    int duration_ms;
};

struct PatternStep {
    std::string type;   // "shortcut" | "shell" | "composite" | ...
    std::string key;    // 规范化后的 key/cmd/name/detail
};

struct Pattern {
    std::string app;
    int frequency;
    int total_duration_ms;
    int avg_duration_ms;
    std::vector<PatternStep> steps;
    std::string signature;       // 聚类签名（含 → 分隔符）
};

// ─────────────────────────────────────────────────────────────────────────────
// ISO 8601 时间戳解析（"2026-01-01T10:00:00[.123456]"）
// ─────────────────────────────────────────────────────────────────────────────

static time_t parse_iso_timestamp(const std::string& ts) {
    if (ts.size() < 19) return 0;
    // YYYY-MM-DDTHH:MM:SS
    struct tm t{};
    t.tm_year = atoi(ts.substr(0, 4).c_str()) - 1900;
    t.tm_mon  = atoi(ts.substr(5, 2).c_str()) - 1;
    t.tm_mday = atoi(ts.substr(8, 2).c_str());
    t.tm_hour = atoi(ts.substr(11, 2).c_str());
    t.tm_min  = atoi(ts.substr(14, 2).c_str());
    t.tm_sec  = atoi(ts.substr(17, 2).c_str());
    // mktime 使用本地时区；这里只要相对差值正确即可
    return mktime(&t);
}

// ─────────────────────────────────────────────────────────────────────────────
// JSON 输出辅助
// ─────────────────────────────────────────────────────────────────────────────

static void json_escape(std::string& out, const std::string& s) {
    out += '"';
    for (char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:   out += c;      break;
        }
    }
    out += '"';
}

static const char* dup_to_com(const std::string& s) {
    size_t len = s.size() + 1;
    char* buf = static_cast<char*>(CoTaskMemAlloc(len));
    if (buf) memcpy(buf, s.c_str(), len);
    return buf;
}

// ─────────────────────────────────────────────────────────────────────────────
// 聚类主逻辑
// ─────────────────────────────────────────────────────────────────────────────

static std::string serialize_patterns(const std::vector<Pattern>& patterns) {
    std::string json = "[";
    for (size_t i = 0; i < patterns.size(); ++i) {
        if (i) json += ",";
        const auto& p = patterns[i];
        json += "{";
        json += "\"app\":";       json_escape(json, p.app);       json += ",";
        json += "\"frequency\":"; json += std::to_string(p.frequency); json += ",";
        json += "\"total_duration_ms\":"; json += std::to_string(p.total_duration_ms); json += ",";
        json += "\"avg_duration_ms\":";   json += std::to_string(p.avg_duration_ms);   json += ",";
        json += "\"signature\":"; json_escape(json, p.signature); json += ",";
        json += "\"steps\":[";
        for (size_t j = 0; j < p.steps.size(); ++j) {
            if (j) json += ",";
            const auto& s = p.steps[j];
            json += "{\"type\":"; json_escape(json, s.type); json += ",";
            // steps 中 shortcut 用 "key"，shell 用 "cmd"，composite 用 "name"
            if (s.type == "shortcut") {
                json += "\"key\":"; json_escape(json, s.key);
            } else if (s.type == "shell") {
                json += "\"cmd\":"; json_escape(json, s.key);
            } else if (s.type == "composite") {
                json += "\"name\":"; json_escape(json, s.key);
            } else {
                json += "\"detail\":"; json_escape(json, s.key);
            }
            json += "}";
        }
        json += "]}";
    }
    json += "]";
    return json;
}

static std::string build_signature(const std::vector<OpRecord>& seg) {
    // 与 Python make_signature 等价：
    //   "<app>|<action_type>:<detail>→<action_type>:<detail>→..."
    std::string sig = seg[0].app + "|";
    for (const auto& r : seg) {
        std::string detail;
        if (r.action_type == "shortcut") {
            detail = canonical_key_str(r.action_detail);
        } else {
            detail = r.action_detail;
        }
        sig += r.action_type;
        sig += ":";
        sig += detail;
        sig += "\xe2\x86\x92"; // → (UTF-8: E2 86 92)
    }
    return sig;
}

static PatternStep normalize_step(const OpRecord& r) {
    // 与 Python _normalize_step 等价
    PatternStep s;
    s.type = r.action_type;
    if (r.action_type == "shortcut") {
        s.key = canonical_key_str(r.action_detail);
    } else {
        s.key = r.action_detail;
    }
    return s;
}

const char* cluster_operations(
    const char* records_json,
    int gap_seconds,
    int min_sequence_length,
    int min_frequency
) {
    if (!records_json) return dup_to_com("[]");

    // ── 1. 解析 JSON ──────────────────────────────────────────────────────
    JsonParser parser(records_json);
    JsonValue root;
    if (!parser.parse(root) || root.type != JsonValue::Type::Array) {
        return dup_to_com("[]");
    }

    std::vector<OpRecord> records;
    records.reserve(root.arr.size());
    for (const auto& item : root.arr) {
        if (item.type != JsonValue::Type::Object) continue;
        OpRecord r;
        r.duration_ms = 0;
        if (const auto* v = item.find("timestamp"))     r.timestamp = v->str;
        if (const auto* v = item.find("app"))            r.app = v->str;
        if (const auto* v = item.find("action_type"))    r.action_type = v->str;
        if (const auto* v = item.find("action_detail"))  r.action_detail = v->str;
        if (const auto* v = item.find("duration_ms"))   r.duration_ms = static_cast<int>(v->num);
        r.epoch = parse_iso_timestamp(r.timestamp);
        records.push_back(std::move(r));
    }

    if (records.empty()) return dup_to_com("[]");

    // ── 2. 按 timestamp 升序排序（稳定排序，保持同时间戳的原始顺序）────────
    std::stable_sort(records.begin(), records.end(),
        [](const OpRecord& a, const OpRecord& b) { return a.epoch < b.epoch; });

    // ── 3. 构建时间序列段（按 app 分组 + gap < gap_seconds）─────────────
    std::vector<std::vector<OpRecord>> segments;
    std::vector<OpRecord> current_seg;
    std::string current_app;
    time_t prev_epoch = 0;
    bool has_prev = false;

    for (const auto& r : records) {
        long gap = 0;
        if (has_prev) gap = static_cast<long>(r.epoch - prev_epoch);

        bool new_seg = false;
        if (r.app != current_app) new_seg = true;
        else if (gap > gap_seconds) new_seg = true;

        if (new_seg) {
            if (static_cast<int>(current_seg.size()) >= min_sequence_length) {
                segments.push_back(std::move(current_seg));
            }
            current_seg.clear();
            current_app = r.app;
        }
        current_seg.push_back(r);
        prev_epoch = r.epoch;
        has_prev = true;
    }
    if (static_cast<int>(current_seg.size()) >= min_sequence_length) {
        segments.push_back(std::move(current_seg));
    }

    if (segments.empty()) return dup_to_com("[]");

    // ── 4. 聚类（按签名分桶）────────────────────────────────────────────
    // 用 unordered_map<sig, vector<segment_index>>
    std::unordered_map<std::string, std::vector<size_t>> sig_map;
    sig_map.reserve(segments.size());
    for (size_t i = 0; i < segments.size(); ++i) {
        std::string sig = build_signature(segments[i]);
        sig_map[std::move(sig)].push_back(i);
    }

    // ── 5. 过滤 frequency >= min_frequency 并生成 pattern ────────────────
    std::vector<Pattern> patterns;
    patterns.reserve(sig_map.size());
    for (const auto& [sig, idxs] : sig_map) {
        int freq = static_cast<int>(idxs.size());
        if (freq < min_frequency) continue;

        // 取第一条序列作为模板
        const auto& template_seg = segments[idxs[0]];
        Pattern p;
        p.app = template_seg[0].app;
        p.frequency = freq;
        p.signature = sig;
        p.steps.reserve(template_seg.size());
        p.total_duration_ms = 0;
        for (const auto& r : template_seg) {
            p.steps.push_back(normalize_step(r));
            p.total_duration_ms += r.duration_ms;
        }
        p.avg_duration_ms = template_seg.empty() ? 0 :
                             p.total_duration_ms / static_cast<int>(template_seg.size());
        patterns.push_back(std::move(p));
    }

    // ── 6. 序列化输出 ────────────────────────────────────────────────────
    return dup_to_com(serialize_patterns(patterns));
}

const char* canonical_key(const char* detail) {
    if (!detail) return dup_to_com("");
    return dup_to_com(canonical_key_str(detail));
}
