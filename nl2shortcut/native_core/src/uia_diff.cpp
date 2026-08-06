/// UIA 树节点级 diff 引擎。
///
/// 输入两个 uia_snapshot() 生成的 JSON 字符串，输出节点级差异。
///
/// diff 算法：
///   1. 递归下降 JSON 解析器把两份 JSON 解析为内部节点树
///   2. 用复合键 (name + control_type + role) 匹配前后节点
///   3. 对匹配成功的节点，逐字段对比，记录字段变化
///   4. 未匹配的 before 节点 → removed；未匹配的 after 节点 → added
///   5. 递归处理 children
///
/// 输出 JSON 格式：
///   {
///     "changed": [ {"path":"...","field":"name","before":"X","after":"Y"} ],
///     "added":   [ {"path":"...","node":{...}} ],
///     "removed": [ {"path":"...","node":{...}} ],
///     "summary": {"changed":N,"added":N,"removed":N,"total_before":N,"total_after":N}
///   }

#include "uia.h"
#include "inject.h"  // for free_result
#include <Windows.h>
#include <string>
#include <vector>
#include <map>
#include <memory>
#include <cstring>
#include <cstdlib>

// ── JSON 值类型 ────────────────────────────────────────────────────

struct JsonValue {
    enum Type { Null, Bool, Number, String, Object, Array };
    Type type = Null;
    bool bool_val = false;
    double num_val = 0.0;
    std::string str_val;
    std::vector<JsonValue> arr;                  // Array
    std::vector<std::pair<std::string, JsonValue>> obj;  // Object (保持插入顺序)

    const JsonValue* find(const std::string& key) const {
        for (auto& kv : obj)
            if (kv.first == key) return &kv.second;
        return nullptr;
    }
};

// ── 递归下降 JSON 解析器（极简版，仅支持 uia_snapshot 输出格式） ────

class JsonParser {
public:
    JsonParser(const char* s) : s_(s), p_(s) {}

    bool parse(JsonValue& out) {
        skip_ws();
        if (!parse_value(out)) return false;
        skip_ws();
        return true;
    }

private:
    const char* s_;
    const char* p_;

    void skip_ws() {
        while (*p_ == ' ' || *p_ == '\t' || *p_ == '\n' || *p_ == '\r') p_++;
    }

    bool parse_value(JsonValue& v) {
        skip_ws();
        char c = *p_;
        if (c == 0) return false;
        if (c == '{') return parse_object(v);
        if (c == '[') return parse_array(v);
        if (c == '"') { v.type = JsonValue::String; return parse_string(v.str_val); }
        if (c == 't' || c == 'f') return parse_bool(v);
        if (c == 'n') return parse_null(v);
        return parse_number(v);
    }

    bool parse_object(JsonValue& v) {
        v.type = JsonValue::Object;
        v.obj.clear();
        p_++;  // {
        skip_ws();
        if (*p_ == '}') { p_++; return true; }
        while (true) {
            skip_ws();
            if (*p_ != '"') return false;
            std::string key;
            if (!parse_string(key)) return false;
            skip_ws();
            if (*p_ != ':') return false;
            p_++;
            JsonValue val;
            if (!parse_value(val)) return false;
            v.obj.emplace_back(std::move(key), std::move(val));
            skip_ws();
            if (*p_ == ',') { p_++; continue; }
            if (*p_ == '}') { p_++; return true; }
            return false;
        }
    }

    bool parse_array(JsonValue& v) {
        v.type = JsonValue::Array;
        v.arr.clear();
        p_++;  // [
        skip_ws();
        if (*p_ == ']') { p_++; return true; }
        while (true) {
            JsonValue val;
            if (!parse_value(val)) return false;
            v.arr.push_back(std::move(val));
            skip_ws();
            if (*p_ == ',') { p_++; continue; }
            if (*p_ == ']') { p_++; return true; }
            return false;
        }
    }

    bool parse_string(std::string& out) {
        if (*p_ != '"') return false;
        p_++;
        out.clear();
        while (*p_ && *p_ != '"') {
            if (*p_ == '\\') {
                p_++;
                switch (*p_) {
                    case '"':  out += '"'; break;
                    case '\\': out += '\\'; break;
                    case '/':  out += '/'; break;
                    case 'n':  out += '\n'; break;
                    case 'r':  out += '\r'; break;
                    case 't':  out += '\t'; break;
                    case 'b':  out += '\b'; break;
                    case 'f':  out += '\f'; break;
                    case 'u': {
                        // \uXXXX 转 UTF-8
                        if (p_[1] && p_[2] && p_[3] && p_[4]) {
                            unsigned cp = 0;
                            for (int i = 1; i <= 4; i++) {
                                char hc = p_[i];
                                cp <<= 4;
                                if (hc >= '0' && hc <= '9') cp |= (hc - '0');
                                else if (hc >= 'a' && hc <= 'f') cp |= (hc - 'a' + 10);
                                else if (hc >= 'A' && hc <= 'F') cp |= (hc - 'A' + 10);
                            }
                            p_ += 4;
                            // 简单 BMP → UTF-8
                            if (cp < 0x80) out += (char)cp;
                            else if (cp < 0x800) {
                                out += (char)(0xC0 | (cp >> 6));
                                out += (char)(0x80 | (cp & 0x3F));
                            } else {
                                out += (char)(0xE0 | (cp >> 12));
                                out += (char)(0x80 | ((cp >> 6) & 0x3F));
                                out += (char)(0x80 | (cp & 0x3F));
                            }
                        }
                        break;
                    }
                    default: out += *p_; break;
                }
                p_++;
            } else {
                out += *p_;
                p_++;
            }
        }
        if (*p_ != '"') return false;
        p_++;
        return true;
    }

    bool parse_number(JsonValue& v) {
        char* end = nullptr;
        v.num_val = strtod(p_, &end);
        if (end == p_) return false;
        v.type = JsonValue::Number;
        p_ = end;
        return true;
    }

    bool parse_bool(JsonValue& v) {
        if (strncmp(p_, "true", 4) == 0) {
            v.type = JsonValue::Bool; v.bool_val = true; p_ += 4; return true;
        }
        if (strncmp(p_, "false", 5) == 0) {
            v.type = JsonValue::Bool; v.bool_val = false; p_ += 5; return true;
        }
        return false;
    }

    bool parse_null(JsonValue& v) {
        if (strncmp(p_, "null", 4) == 0) {
            v.type = JsonValue::Null; p_ += 4; return true;
        }
        return false;
    }
};

// ── JSON 序列化（用于 diff 输出） ──────────────────────────────────

static void serialize_string(std::string& out, const std::string& s) {
    out += '"';
    for (char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if ((unsigned char)c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    out += '"';
}

static void serialize_value(std::string& out, const JsonValue& v) {
    switch (v.type) {
        case JsonValue::Null:   out += "null"; break;
        case JsonValue::Bool:   out += v.bool_val ? "true" : "false"; break;
        case JsonValue::Number: {
            char buf[64];
            snprintf(buf, sizeof(buf), "%g", v.num_val);
            out += buf;
            break;
        }
        case JsonValue::String: serialize_string(out, v.str_val); break;
        case JsonValue::Object:
            out += "{";
            for (size_t i = 0; i < v.obj.size(); i++) {
                if (i) out += ",";
                serialize_string(out, v.obj[i].first);
                out += ":";
                serialize_value(out, v.obj[i].second);
            }
            out += "}";
            break;
        case JsonValue::Array:
            out += "[";
            for (size_t i = 0; i < v.arr.size(); i++) {
                if (i) out += ",";
                serialize_value(out, v.arr[i]);
            }
            out += "]";
            break;
    }
}

// ── diff 引擎 ─────────────────────────────────────────────────────

struct DiffResult {
    std::string changed_json;  // 变化字段数组
    std::string added_json;    // 新增节点数组
    std::string removed_json;  // 删除节点数组
    int changed_count = 0;
    int added_count = 0;
    int removed_count = 0;
    int total_before = 0;
    int total_after = 0;
};

// 节点匹配键：name + control_type + role（兜底用索引）
static std::string node_key(const JsonValue* node, size_t idx) {
    if (!node || node->type != JsonValue::Object) return "#" + std::to_string(idx);
    const JsonValue* name = node->find("name");
    const JsonValue* ct = node->find("control_type");
    const JsonValue* role = node->find("role");
    std::string k;
    if (name && name->type == JsonValue::String) k = name->str_val;
    k += "|";
    if (ct && ct->type == JsonValue::String) k += ct->str_val;
    k += "|";
    if (role && role->type == JsonValue::String) k += role->str_val;
    if (k == "||") k = "#" + std::to_string(idx);
    return k;
}

// 对比字段列表（顺序重要）
static const char* DIFF_FIELDS[] = {
    "name", "value", "state", "x", "y", "width", "height",
    "patterns", "keyboard_shortcut"
};

// 元字段（出现在快照顶层，不在 root 节点内）：UIA_FILTER_META_FIELDS 时跳过
static const char* META_FIELDS[] = {
    "elapsed_ms", "focus",
};

// ── 过滤辅助函数 ────────────────────────────────────────────────────

// state 字段的"焦点相关"变化过滤
static bool is_focus_state_diff(const std::string& bs, const std::string& as) {
    // 去掉外层引号
    auto strip = [](const std::string& s) -> std::string {
        if (s.size() >= 2 && s.front() == '"' && s.back() == '"')
            return s.substr(1, s.size() - 2);
        return s;
    };
    std::string b = strip(bs);
    std::string a = strip(as);
    if (b == a) return true;

    // 从 state 字符串中剥去所有焦点相关 token（focused / focusable）
    // 然后对比剥离后的剩余部分；如果相同 → 只是焦点差异 → 过滤
    auto strip_all_focus = [](const std::string& s) {
        std::string out;
        size_t i = 0;
        while (i < s.size()) {
            if (i + 9 <= s.size() &&
                (s.compare(i, 9, "focusable") == 0
                 || s.compare(i, 9, "Focusable") == 0)) {
                i += 9;
                // 跳过紧随其后的分隔符
                if (i < s.size() && s[i] == ',') i++;
                continue;
            }
            if (i + 7 <= s.size() &&
                (s.compare(i, 7, "focused") == 0
                 || s.compare(i, 7, "Focused") == 0)) {
                i += 7;
                if (i < s.size() && s[i] == ',') i++;
                continue;
            }
            out += s[i];
            i++;
        }
        // 清理首尾逗号和连续逗号
        std::string clean;
        for (size_t j = 0; j < out.size(); j++) {
            char c = out[j];
            if (c == ',' && (j == 0 || j == out.size() - 1)) continue;
            if (c == ',' && j + 1 < out.size() && out[j+1] == ',') continue;
            clean += c;
        }
        return clean;
    };

    std::string b_clean = strip_all_focus(b);
    std::string a_clean = strip_all_focus(a);
    // 任一原来就含焦点关键词，且剥离后两边相等 → 判定为纯焦点差异
    bool any_focus = (b.find("focus") != std::string::npos || b.find("Focus") != std::string::npos
                   || a.find("focus") != std::string::npos || a.find("Focus") != std::string::npos);
    return any_focus && (b_clean == a_clean);
}

// 位置/尺寸抖动过滤
static bool is_position_tolerance_diff(
    const std::string& field, const std::string& bs, const std::string& as,
    int tolerance_px) {
    if (tolerance_px <= 0) return false;
    if (field != "x" && field != "y" && field != "width" && field != "height") return false;
    // 尝试解析成数字
    try {
        double b = std::stod(bs);
        double a = std::stod(as);
        return std::abs(b - a) <= (double)tolerance_px;
    } catch (...) {
        return false;
    }
}

// ── 带过滤的 diff 函数 ─────────────────────────────────────────────

struct DiffConfig {
    int filter_flags = 0;
    int position_tolerance_px = 10;
    int filtered_focus = 0;       // 被过滤的焦点差异数（统计）
    int filtered_position = 0;    // 被过滤的位置抖动数（统计）
    int filtered_meta = 0;        // 被过滤的元字段差异数（统计）
};

static void diff_children(const JsonValue* before_arr, const JsonValue* after_arr,
                          const std::string& path, DiffResult& dr, DiffConfig& cfg);

static void diff_node(const JsonValue* before, const JsonValue* after,
                      const std::string& path, DiffResult& dr, DiffConfig& cfg) {
    if (!before || !after) return;

    // 对比标量字段
    for (const char* field : DIFF_FIELDS) {
        const JsonValue* bv = before->find(field);
        const JsonValue* av = after->find(field);
        if (!bv && !av) continue;

        // 序列化两个值做字符串对比
        std::string bs, as;
        if (bv) serialize_value(bs, *bv);
        else bs = "null";
        if (av) serialize_value(as, *av);
        else as = "null";

        if (bs == as) continue;

        // ── 过滤器：焦点状态 ──
        if ((cfg.filter_flags & 0x01) && field == std::string("state")) {
            if (is_focus_state_diff(bs, as)) {
                cfg.filtered_focus++;
                continue;  // 跳过，不计入 changed
            }
        }
        // ── 过滤器：位置抖动 ──
        if (cfg.filter_flags & 0x02) {
            if (is_position_tolerance_diff(field, bs, as, cfg.position_tolerance_px)) {
                cfg.filtered_position++;
                continue;
            }
        }

        // 真正有差异 → 记录
        if (dr.changed_count > 0) dr.changed_json += ",";
        dr.changed_json += "{\"path\":";
        serialize_string(dr.changed_json, path);
        dr.changed_json += ",\"field\":\"";
        dr.changed_json += field;
        dr.changed_json += "\",\"before\":";
        dr.changed_json += bs;
        dr.changed_json += ",\"after\":";
        dr.changed_json += as;
        dr.changed_json += "}";
        dr.changed_count++;
    }

    // 递归对比 children
    const JsonValue* bc = before->find("children");
    const JsonValue* ac = after->find("children");
    diff_children(bc, ac, path, dr, cfg);
}

static void diff_children(const JsonValue* before_arr, const JsonValue* after_arr,
                          const std::string& path, DiffResult& dr, DiffConfig& cfg) {
    // 收集 before 节点
    std::vector<const JsonValue*> before_nodes;
    if (before_arr && before_arr->type == JsonValue::Array) {
        for (auto& n : before_arr->arr) {
            if (n.type == JsonValue::Object) before_nodes.push_back(&n);
        }
    }
    // 收集 after 节点
    std::vector<const JsonValue*> after_nodes;
    if (after_arr && after_arr->type == JsonValue::Array) {
        for (auto& n : after_arr->arr) {
            if (n.type == JsonValue::Object) after_nodes.push_back(&n);
        }
    }

    dr.total_before += (int)before_nodes.size();
    dr.total_after += (int)after_nodes.size();

    // 用复合键匹配：before[i] 对应 after 中同键的第一个未匹配节点
    std::vector<bool> after_matched(after_nodes.size(), false);
    std::vector<int> match_after(before_nodes.size(), -1);  // after index for before[i]

    // 第一轮：按复合键匹配
    std::map<std::string, std::vector<size_t>> after_key_map;
    for (size_t i = 0; i < after_nodes.size(); i++) {
        std::string k = node_key(after_nodes[i], i);
        after_key_map[k].push_back(i);
    }
    for (size_t i = 0; i < before_nodes.size(); i++) {
        std::string k = node_key(before_nodes[i], i);
        auto it = after_key_map.find(k);
        if (it != after_key_map.end()) {
            for (size_t j : it->second) {
                if (!after_matched[j]) {
                    after_matched[j] = true;
                    match_after[i] = (int)j;
                    break;
                }
            }
        }
    }

    // 第二轮：未匹配的 before 用索引兜底（仅当索引 < after 数量且未匹配）
    for (size_t i = 0; i < before_nodes.size(); i++) {
        if (match_after[i] >= 0) continue;
        if (i < after_nodes.size() && !after_matched[i]) {
            after_matched[i] = true;
            match_after[i] = (int)i;
        }
    }

    // 处理匹配的节点：递归 diff
    for (size_t i = 0; i < before_nodes.size(); i++) {
        int j = match_after[i];
        if (j < 0) continue;
        std::string child_path = path + "/" + node_key(before_nodes[i], i);
        diff_node(before_nodes[i], after_nodes[j], child_path, dr, cfg);
    }

    // 未匹配的 before → removed
    for (size_t i = 0; i < before_nodes.size(); i++) {
        if (match_after[i] >= 0) continue;
        if (dr.removed_count > 0) dr.removed_json += ",";
        dr.removed_json += "{\"path\":";
        serialize_string(dr.removed_json, path + "/" + node_key(before_nodes[i], i));
        dr.removed_json += ",\"node\":";
        serialize_value(dr.removed_json, *before_nodes[i]);
        dr.removed_json += "}";
        dr.removed_count++;
    }

    // 未匹配的 after → added
    for (size_t j = 0; j < after_nodes.size(); j++) {
        if (after_matched[j]) continue;
        if (dr.added_count > 0) dr.added_json += ",";
        dr.added_json += "{\"path\":";
        serialize_string(dr.added_json, path + "/" + node_key(after_nodes[j], j));
        dr.added_json += ",\"node\":";
        serialize_value(dr.added_json, *after_nodes[j]);
        dr.added_json += "}";
        dr.added_count++;
    }
}

// ── 公开入口 ──────────────────────────────────────────────────────

static const char* _uia_diff_impl(
    const char* before_json, const char* after_json,
    DiffConfig& cfg) {
    if (!before_json || !after_json) return nullptr;

    JsonValue before_obj, after_obj;
    JsonParser bp(before_json);
    if (!bp.parse(before_obj)) return nullptr;
    JsonParser ap(after_json);
    if (!ap.parse(after_obj)) return nullptr;

    DiffResult dr;

    // 顶层元字段过滤 (UIA_FILTER_META_FIELDS)
    if (cfg.filter_flags & 0x04) {
        for (const char* meta : META_FIELDS) {
            const JsonValue* bm = before_obj.find(meta);
            const JsonValue* am = after_obj.find(meta);
            if (!bm && !am) continue;
            std::string bs, as;
            if (bm) serialize_value(bs, *bm);
            else bs = "null";
            if (am) serialize_value(as, *am);
            else as = "null";
            if (bs != as) cfg.filtered_meta++;
        }
    }

    // uia_snapshot 输出结构：{app_name, root, focus, elapsed_ms}
    // 子树在 "root" 字段下；若存在则从 root 开始 diff，
    // 否则兜底直接 diff 整个对象。
    const JsonValue* before_root = before_obj.find("root");
    const JsonValue* after_root = after_obj.find("root");
    if (before_root && after_root
        && before_root->type == JsonValue::Object
        && after_root->type == JsonValue::Object) {
        diff_node(before_root, after_root, "", dr, cfg);
    } else {
        diff_node(&before_obj, &after_obj, "", dr, cfg);
    }

    // 构造完整 JSON 输出（summary 额外加过滤统计字段）
    std::string out;
    out += "{\"changed\":[";
    out += dr.changed_json;
    out += "],\"added\":[";
    out += dr.added_json;
    out += "],\"removed\":[";
    out += dr.removed_json;
    out += "],\"summary\":{\"changed\":";
    out += std::to_string(dr.changed_count);
    out += ",\"added\":";
    out += std::to_string(dr.added_count);
    out += ",\"removed\":";
    out += std::to_string(dr.removed_count);
    out += ",\"total_before\":";
    out += std::to_string(dr.total_before);
    out += ",\"total_after\":";
    out += std::to_string(dr.total_after);
    out += ",\"filtered_focus\":";
    out += std::to_string(cfg.filtered_focus);
    out += ",\"filtered_position\":";
    out += std::to_string(cfg.filtered_position);
    out += ",\"filtered_meta\":";
    out += std::to_string(cfg.filtered_meta);
    out += "}}";

    // CoTaskMemAlloc 分配（跨 DLL 安全）
    size_t len = out.size() + 1;
    char* buf = (char*)CoTaskMemAlloc(len);
    if (buf) memcpy(buf, out.c_str(), len);
    return buf;
}

const char* uia_diff(const char* before_json, const char* after_json) {
    DiffConfig cfg;  // 零过滤
    return _uia_diff_impl(before_json, after_json, cfg);
}

const char* uia_diff_filtered(
    const char* before_json, const char* after_json,
    int filter_flags, int position_tolerance_px) {
    DiffConfig cfg;
    cfg.filter_flags = filter_flags;
    cfg.position_tolerance_px = position_tolerance_px > 0 ? position_tolerance_px : 10;
    return _uia_diff_impl(before_json, after_json, cfg);
}
