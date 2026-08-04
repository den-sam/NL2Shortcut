/// 窗口检测 —— C++ 原生 Win32 API，替代 Python ctypes。

#include "window.h"
#include "inject.h"   // for free_result
#include <Windows.h>
#include <string>
#include <vector>
#include <TlHelp32.h>

#pragma comment(lib, "user32.lib")
#pragma comment(lib, "kernel32.lib")

static std::string utf16_to_utf8(const wchar_t* wstr) {
    if (!wstr) return "";
    int len = WideCharToMultiByte(CP_UTF8, 0, wstr, -1, nullptr, 0, nullptr, nullptr);
    if (len <= 0) return "";
    std::vector<char> buf(len);
    WideCharToMultiByte(CP_UTF8, 0, wstr, -1, buf.data(), len, nullptr, nullptr);
    return buf.data();
}

static std::string get_process_name(DWORD pid) {
    if (pid == 0) return "";
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return "";
    PROCESSENTRY32W pe{ sizeof(pe) };
    std::string result;
    if (Process32FirstW(snap, &pe)) {
        do {
            if (pe.th32ProcessID == pid) {
                result = utf16_to_utf8(pe.szExeFile);
                break;
            }
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    return result;
}

// 简易 JSON 字符串转义
static void json_escape(std::string& out, const std::string& s) {
    out += '"';
    for (char c : s) {
        if (c == '"')  out += "\\\"";
        else if (c == '\\') out += "\\\\";
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else out += c;
    }
    out += '"';
}

const char* foreground_window_json() {
    HWND hwnd = GetForegroundWindow();
    if (!hwnd) return nullptr;

    WCHAR title_buf[512]{};
    GetWindowTextW(hwnd, title_buf, 512);
    std::string title = utf16_to_utf8(title_buf);

    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    std::string pname = get_process_name(pid);

    std::string json;
    json += "{";
    json += "\"title\":"; json_escape(json, title); json += ",";
    json += "\"process_name\":\""; json += pname; json += "\",";
    json += "\"process_id\":"; json += std::to_string(pid); json += ",";
    json += "\"hwnd\":"; json += std::to_string((unsigned long long)(ULONG_PTR)hwnd); json += ",";
    json += "\"platform\":\"windows\"";
    json += "}";

    size_t len = json.size() + 1;
    char* buf = (char*)CoTaskMemAlloc(len);
    if (buf) memcpy(buf, json.c_str(), len);
    return buf;
}
