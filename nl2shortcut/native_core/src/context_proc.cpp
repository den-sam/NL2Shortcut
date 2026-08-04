/// 应用上下文检测 —— C++ 原生 Win32 实现。
///
/// 替代 Python context.py 中的 _detect_windows() + _get_process_name()。
/// 原实现依赖 psutil（间接走 Win32 API + Python 转换层）或 subprocess tasklist
/// （50-200ms/次）。本模块直接用 QueryFullProcessImageNameW，<1ms/次，无需管理员权限。
///
/// 与 window.cpp 中的 foreground_window_json() 的区别：
///   - window.cpp 用 CreateToolhelp32Snapshot 遍历进程表（O(N)，N≈200-500），
///     且只返回原始 process_name，不映射 app_name。
///   - 本模块用 QueryFullProcessImageNameW（O(1)），同时返回 app_name 友好名。

#include "context_proc.h"
#include "inject.h"   // for free_result

#include <Windows.h>

#include <string>
#include <vector>
#include <cstring>
#include <cctype>

#pragma comment(lib, "kernel32.lib")
#pragma comment(lib, "user32.lib")

// ─────────────────────────────────────────────────────────────────────────────
// 字符串工具
// ─────────────────────────────────────────────────────────────────────────────

static std::string utf16_to_utf8(const wchar_t* wstr) {
    if (!wstr) return "";
    int len = WideCharToMultiByte(CP_UTF8, 0, wstr, -1, nullptr, 0, nullptr, nullptr);
    if (len <= 0) return "";
    std::vector<char> buf(static_cast<size_t>(len));
    WideCharToMultiByte(CP_UTF8, 0, wstr, -1, buf.data(), len, nullptr, nullptr);
    return std::string(buf.data());
}

static std::string to_lower_ascii(const std::string& s) {
    std::string out = s;
    for (char& c : out) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c + ('a' - 'A'));
    }
    return out;
}

/// 取路径 basename（不含目录、不含 .exe 后缀），返回小写。
/// 例："C:\\Program Files\\Google\\Chrome\\chrome.exe" → "chrome"
static std::string basename_no_ext_lower(const std::string& path) {
    size_t slash = path.find_last_of("\\/");
    std::string fname = (slash == std::string::npos) ? path : path.substr(slash + 1);
    // 去掉 .exe 后缀（不区分大小写）
    std::string lower_fname = to_lower_ascii(fname);
    const std::string ext = ".exe";
    if (lower_fname.size() > ext.size() &&
        lower_fname.compare(lower_fname.size() - ext.size(), ext.size(), ext) == 0) {
        fname = fname.substr(0, fname.size() - ext.size());
    }
    return to_lower_ascii(fname);
}

// ─────────────────────────────────────────────────────────────────────────────
// 应用指纹表（与 context.py::_APP_FINGERPRINTS 同步）
// ─────────────────────────────────────────────────────────────────────────────
//
// 第一列是「进程名 stem 小写的子串匹配」，第二列是友好名称。
// 例：进程名 "Code.exe" → stem "code" → 包含 "code" → "vscode"
//
// 顺序很重要：更具体的关键字应放在前面（如 "cursor" 在 "code" 之前会被先匹配，
// 但这里两者独立，互不包含，故顺序无关）。我们用一个静态数组即可。

struct Fingerprint {
    const char* keyword;
    const char* friendly;
};

static const Fingerprint kFingerprints[] = {
    {"cursor",         "vscode"},
    {"code",           "vscode"},
    {"chrome",         "chrome"},
    {"msedge",         "edge"},
    {"firefox",        "firefox"},
    {"windowsterminal","terminal"},
    {"wezterm",        "terminal"},
    {"alacritty",      "terminal"},
    {"conhost",        "terminal"},
    {"powershell",     "terminal"},
    {"cmd",            "terminal"},
    {"explorer",       "explorer"},
    {"notepad++",      "notepad++"},
    {"notepad",        "notepad"},
    {"devenv",         "visual_studio"},
    {"idea64",         "intellij"},
    {"pycharm64",      "pycharm"},
    {"webstorm64",     "webstorm"},
    {"sublime_text",   "sublime"},
    {"obsidian",       "obsidian"},
    {"slack",          "slack"},
    {"teams",          "teams"},
    {"discord",        "discord"},
    {"wechat",         "wechat"},
    {"qq",             "qq"},
    {"dingtalk",       "dingtalk"},
    {"outlook",        "outlook"},
    {"thunderbird",    "thunderbird"},
    {"spotify",        "spotify"},
    {"photoshop",      "photoshop"},
    {"illustrator",    "illustrator"},
    {"figma",          "figma"},
    {"blender",        "blender"},
    {"excel",          "excel"},
    {"winword",        "word"},
    {"powerpnt",       "powerpoint"},
    {"acrord32",       "acrobat"},
    {"foxit",          "acrobat"},
    {"putty",          "terminal"},
    {"mobaxterm",      "terminal"},
};

static std::string fingerprint_impl(const std::string& stem_lower) {
    if (stem_lower.empty()) return "unknown";
    for (const auto& fp : kFingerprints) {
        if (strstr(stem_lower.c_str(), fp.keyword) != nullptr) {
            return fp.friendly;
        }
    }
    return stem_lower;
}

// ─────────────────────────────────────────────────────────────────────────────
// JSON 字符串转义（与 window.cpp 同款，独立实现避免耦合）
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

// ─────────────────────────────────────────────────────────────────────────────
// 内存分配 —— CoTaskMemAlloc，与 free_result() 配套
// ─────────────────────────────────────────────────────────────────────────────

static const char* dup_to_com(const std::string& s) {
    size_t len = s.size() + 1;
    char* buf = static_cast<char*>(CoTaskMemAlloc(len));
    if (buf) memcpy(buf, s.c_str(), len);
    return buf;
}

// ─────────────────────────────────────────────────────────────────────────────
// 进程名获取 —— QueryFullProcessImageNameW（核心加速点）
// ─────────────────────────────────────────────────────────────────────────────
//
// 对比：
//   - CreateToolhelp32Snapshot + Process32First/Next：O(N) 遍历进程表，~1-3ms
//   - psutil.Process(pid).name()：Python 层开销 + 间接 Win32 调用，~3-10ms
//   - subprocess tasklist：fork + 解析 CSV，~50-200ms
//   - QueryFullProcessImageNameW：直接 PID 查询，<1ms，无需管理员权限
//
// 注：PROCESS_QUERY_LIMITED_INFORMATION 自 Vista 起可用，允许在非管理员进程下
// 查询大部分进程的镜像路径（少数受保护进程除外）。

static std::string get_process_image_path(DWORD pid) {
    if (pid == 0) return "";
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return "";
    wchar_t buf[MAX_PATH] = {0};
    DWORD size = MAX_PATH;
    BOOL ok = QueryFullProcessImageNameW(h, 0, buf, &size);
    CloseHandle(h);
    if (!ok) return "";
    return utf16_to_utf8(buf);
}

// ─────────────────────────────────────────────────────────────────────────────
// 导出函数实现
// ─────────────────────────────────────────────────────────────────────────────

const char* get_process_name_by_pid(uint32_t pid) {
    std::string path = get_process_image_path(static_cast<DWORD>(pid));
    if (path.empty()) {
        // 回退：返回 "pid:<pid>" 形式，与 Python 层一致
        return dup_to_com("pid:" + std::to_string(pid));
    }
    // 取 basename（保留 .exe，与 psutil.Process.name() 一致）
    size_t slash = path.find_last_of("\\/");
    std::string exe_name = (slash == std::string::npos) ? path : path.substr(slash + 1);
    return dup_to_com(exe_name);
}

const char* fingerprint_process(const char* process_name) {
    if (!process_name || !*process_name) {
        return dup_to_com("unknown");
    }
    std::string stem = basename_no_ext_lower(process_name);
    return dup_to_com(fingerprint_impl(stem));
}

const char* get_foreground_context() {
    HWND hwnd = GetForegroundWindow();
    if (!hwnd) return nullptr;

    // ── 标题 ─────────────────────────────────────────────────────────────
    WCHAR title_buf[512] = {0};
    GetWindowTextW(hwnd, title_buf, 512);
    std::string title = utf16_to_utf8(title_buf);

    // ── PID ──────────────────────────────────────────────────────────────
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);

    // ── 进程镜像完整路径 → basename → fingerprint ───────────────────────
    std::string exe_name;
    if (pid != 0) {
        std::string path = get_process_image_path(pid);
        if (!path.empty()) {
            size_t slash = path.find_last_of("\\/");
            exe_name = (slash == std::string::npos) ? path : path.substr(slash + 1);
        }
    }
    std::string stem = basename_no_ext_lower(exe_name);
    std::string app_name = fingerprint_impl(stem);

    // ── 组装 JSON ────────────────────────────────────────────────────────
    std::string json;
    json += "{";
    json += "\"title\":";       json_escape(json, title);       json += ",";
    json += "\"process_name\":"; json_escape(json, exe_name);    json += ",";
    json += "\"process_id\":";   json += std::to_string(pid);    json += ",";
    json += "\"app_name\":";     json_escape(json, app_name);    json += ",";
    json += "\"hwnd\":";         json += std::to_string(reinterpret_cast<uint64_t>(hwnd)); json += ",";
    json += "\"platform\":\"windows\"";
    json += "}";

    return dup_to_com(json);
}
