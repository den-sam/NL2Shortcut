/// C++ 执行内核实现。
///
/// 把"候选键构建 + 执行循环 + 验证 + 重试"整体下沉到 C++。
/// 验证维度：剪贴板文本变化 / 前台窗口标题变化。
///
/// 依赖：
///   - send_hotkey()（来自 inject.cpp）：注入按键
///   - 内置剪贴板读取（OpenClipboard + GetClipboardData）
///   - 内置前台窗口标题读取（GetForegroundWindow + GetWindowTextW）

#include "executor.h"
#include "inject.h"  // for free_result / send_hotkey
#include <Windows.h>
#include <string>
#include <vector>
#include <chrono>
#include <cstring>

// ── 简易 JSON 数组解析（仅解析字符串数组 ["a","b","c"]） ──────────

static std::vector<std::string> parse_string_array(const char* json) {
    std::vector<std::string> result;
    if (!json) return result;
    const char* p = json;
    while (*p && *p != '[') p++;
    if (*p != '[') return result;
    p++;  // skip [
    while (*p) {
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
        if (*p == ']') break;
        if (*p != '"') { p++; continue; }
        p++;  // skip opening "
        std::string s;
        while (*p && *p != '"') {
            if (*p == '\\' && p[1]) {
                switch (p[1]) {
                    case '"':  s += '"'; break;
                    case '\\': s += '\\'; break;
                    case '/':  s += '/'; break;
                    case 'n':  s += '\n'; break;
                    case 'r':  s += '\r'; break;
                    case 't':  s += '\t'; break;
                    default:   s += p[1]; break;
                }
                p += 2;
            } else {
                s += *p;
                p++;
            }
        }
        if (*p == '"') p++;  // skip closing "
        result.push_back(s);
    }
    return result;
}

// ── JSON 字符串转义（用于输出） ─────────────────────────────────────

static void json_escape(std::string& out, const std::string& s) {
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
                    snprintf(buf, sizeof(buf), "\\u%04x", (unsigned char)c);
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    out += '"';
}

// ── 剪贴板文本读取（UTF-8） ────────────────────────────────────────

static std::string read_clipboard_text() {
    if (!OpenClipboard(nullptr)) return "";
    std::string result;
    HANDLE h = GetClipboardData(CF_UNICODETEXT);
    if (h) {
        wchar_t* wstr = (wchar_t*)GlobalLock(h);
        if (wstr) {
            int len = WideCharToMultiByte(CP_UTF8, 0, wstr, -1, nullptr, 0, nullptr, nullptr);
            if (len > 0) {
                std::vector<char> buf(len);
                WideCharToMultiByte(CP_UTF8, 0, wstr, -1, buf.data(), len, nullptr, nullptr);
                result = buf.data();
            }
            GlobalUnlock(h);
        }
    }
    CloseClipboard();
    return result;
}

// ── 前台窗口标题读取（UTF-8） ──────────────────────────────────────

static std::string read_foreground_window_title() {
    HWND hwnd = GetForegroundWindow();
    if (!hwnd) return "";
    wchar_t title[512] = {0};
    int len = GetWindowTextW(hwnd, title, 511);
    if (len <= 0) return "";
    char buf[1024] = {0};
    WideCharToMultiByte(CP_UTF8, 0, title, -1, buf, sizeof(buf), nullptr, nullptr);
    return std::string(buf);
}

// ── 公开入口 ──────────────────────────────────────────────────────

const char* execute_with_retry(
    const char* candidates_json,
    int verify_delay_ms,
    int max_attempts,
    int use_clipboard_check,
    int use_window_check
) {
    auto t0 = std::chrono::high_resolution_clock::now();

    std::vector<std::string> candidates = parse_string_array(candidates_json);
    if (candidates.empty()) {
        // 返回失败结果
        std::string out = "{\"success\":false,\"used_key\":\"\",\"attempts\":0,"
                          "\"error\":\"empty candidates\",\"elapsed_ms\":0,"
                          "\"verifications\":[]}";
        size_t len = out.size() + 1;
        char* buf = (char*)CoTaskMemAlloc(len);
        if (buf) memcpy(buf, out.c_str(), len);
        return buf;
    }

    if (max_attempts <= 0) max_attempts = 3;
    if (max_attempts > (int)candidates.size()) max_attempts = (int)candidates.size();

    // 注入前快照
    std::string clip_before, win_before;
    bool check_clip = (use_clipboard_check != 0);
    bool check_win = (use_window_check != 0);
    if (check_clip) clip_before = read_clipboard_text();
    if (check_win) win_before = read_foreground_window_title();

    // 尝试循环
    bool success = false;
    std::string used_key;
    int attempts = 0;
    std::string error_msg;
    std::string verifications_json;

    for (int i = 0; i < max_attempts && !success; i++) {
        attempts = i + 1;
        const std::string& key = candidates[i];

        // 调用 inject.cpp 的 send_hotkey
        int rc = send_hotkey(key.c_str());
        if (rc != 0) {
            if (!verifications_json.empty()) verifications_json += ",";
            verifications_json += "{\"key\":";
            json_escape(verifications_json, key);
            verifications_json += ",\"attempt\":";
            verifications_json += std::to_string(attempts);
            verifications_json += ",\"verified\":false,\"reason\":\"inject failed (rc=";
            verifications_json += std::to_string(rc);
            verifications_json += ")\"}";
            error_msg = "inject failed on attempt " + std::to_string(attempts);
            continue;
        }

        // 等待验证延迟
        if (verify_delay_ms > 0) {
            Sleep(verify_delay_ms);
        }

        // 验证
        bool verified = false;
        std::string reason;

        if (!check_clip && !check_win) {
            // 无验证维度 → 直接视为成功（类似 noop）
            verified = true;
            reason = "no verification configured";
        } else {
            // 剪贴板验证
            if (!verified && check_clip) {
                std::string clip_after = read_clipboard_text();
                if (clip_after != clip_before) {
                    verified = true;
                    reason = "clipboard changed";
                }
            }
            // 窗口验证
            if (!verified && check_win) {
                std::string win_after = read_foreground_window_title();
                if (win_after != win_before) {
                    verified = true;
                    reason = "window changed";
                }
            }
            if (!verified) {
                reason = "no observable change";
            }
        }

        // 记录验证详情
        if (!verifications_json.empty()) verifications_json += ",";
        verifications_json += "{\"key\":";
        json_escape(verifications_json, key);
        verifications_json += ",\"attempt\":";
        verifications_json += std::to_string(attempts);
        verifications_json += ",\"verified\":";
        verifications_json += verified ? "true" : "false";
        verifications_json += ",\"reason\":";
        json_escape(verifications_json, reason);
        verifications_json += "}";

        if (verified) {
            success = true;
            used_key = key;
        } else {
            error_msg = "verification failed on attempt " + std::to_string(attempts);
        }
    }

    if (!success && error_msg.empty()) {
        error_msg = "all candidates exhausted";
    }

    // 计算耗时
    auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::high_resolution_clock::now() - t0).count() / 1000.0;

    // 构造输出 JSON
    std::string out;
    out += "{\"success\":";
    out += (success ? "true" : "false");
    out += ",\"used_key\":";
    json_escape(out, used_key);
    out += ",\"attempts\":";
    out += std::to_string(attempts);
    out += ",\"error\":";
    if (error_msg.empty()) out += "null";
    else json_escape(out, error_msg);
    out += ",\"elapsed_ms\":";
    char buf[32];
    snprintf(buf, sizeof(buf), "%.3f", elapsed);
    out += buf;
    out += ",\"verifications\":[";
    out += verifications_json;
    out += "]}";

    // CoTaskMemAlloc 分配（跨 DLL 安全）
    size_t len = out.size() + 1;
    char* buf_out = (char*)CoTaskMemAlloc(len);
    if (buf_out) memcpy(buf_out, out.c_str(), len);
    return buf_out;
}
