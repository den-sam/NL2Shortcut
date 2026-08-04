/// 键盘注入引擎 —— C++ 原生 SendInput，快捷键执行层的主实现。
///
/// 设计要点
/// ────────
/// 1. scan-code 模式（wVk=0，只填 wScan）——兼容 DirectInput 游戏与部分受保护程序。
/// 2. 扩展键标志（KEYEVENTF_EXTENDEDKEY）——方向键/Home/End/Delete/Win 等
///    在 PS/2 扫描码体系里属「扩展键」，缺此标志会被识别成小键盘键位，
///    导致 Ctrl+Left、Win+Up、Ctrl+Alt+Delete 等组合行为异常。
/// 3. 一次 SendInput 批量提交整个组合键 —— 相比逐键调用，减少系统调用次数，
///    且避免中途被其他进程的输入插队（原子性更好）。

#include "inject.h"
#include <Windows.h>
#include <cstring>
#include <cstdlib>
#include <string>
#include <vector>

#pragma comment(lib, "user32.lib")

// ── 虚拟键码映射 ──────────────────────────────────────────────────

static WORD resolve_vk(const char* name) {
    if (!name || !*name) return 0;

    // 修饰键
    if (_stricmp(name, "ctrl") == 0 || _stricmp(name, "control") == 0) return VK_CONTROL;
    if (_stricmp(name, "alt") == 0 || _stricmp(name, "option") == 0)   return VK_MENU;
    if (_stricmp(name, "shift") == 0) return VK_SHIFT;
    if (_stricmp(name, "win") == 0 || _stricmp(name, "windows") == 0 ||
        _stricmp(name, "cmd") == 0 || _stricmp(name, "super") == 0 ||
        _stricmp(name, "meta") == 0)
        return VK_LWIN;

    // 单字母 / 数字
    if (strlen(name) == 1) {
        char c = name[0];
        if (c >= 'a' && c <= 'z') return (WORD)(0x41 + (c - 'a'));
        if (c >= 'A' && c <= 'Z') return (WORD)(0x41 + (c - 'A'));
        if (c >= '0' && c <= '9') return (WORD)(0x30 + (c - '0'));
    }

    // F1-F24
    if ((name[0] == 'f' || name[0] == 'F') && name[1] >= '0' && name[1] <= '9') {
        int n = atoi(name + 1);
        if (n >= 1 && n <= 24) return (WORD)(VK_F1 + n - 1);
    }

    // 特殊键
    if (_stricmp(name, "escape") == 0 || _stricmp(name, "esc") == 0)   return VK_ESCAPE;
    if (_stricmp(name, "enter") == 0 || _stricmp(name, "return") == 0)  return VK_RETURN;
    if (_stricmp(name, "tab") == 0)      return VK_TAB;
    if (_stricmp(name, "space") == 0 || _stricmp(name, "spacebar") == 0) return VK_SPACE;
    if (_stricmp(name, "backspace") == 0) return VK_BACK;
    if (_stricmp(name, "delete") == 0 || _stricmp(name, "del") == 0)    return VK_DELETE;
    if (_stricmp(name, "insert") == 0 || _stricmp(name, "ins") == 0)    return VK_INSERT;
    if (_stricmp(name, "home") == 0)     return VK_HOME;
    if (_stricmp(name, "end") == 0)      return VK_END;
    if (_stricmp(name, "pageup") == 0 || _stricmp(name, "pgup") == 0)   return VK_PRIOR;
    if (_stricmp(name, "pagedown") == 0 || _stricmp(name, "pgdn") == 0) return VK_NEXT;
    if (_stricmp(name, "up") == 0)       return VK_UP;
    if (_stricmp(name, "down") == 0)     return VK_DOWN;
    if (_stricmp(name, "left") == 0)     return VK_LEFT;
    if (_stricmp(name, "right") == 0)    return VK_RIGHT;
    if (_stricmp(name, "printscreen") == 0 || _stricmp(name, "prtsc") == 0) return VK_SNAPSHOT;
    if (_stricmp(name, "pause") == 0)    return VK_PAUSE;
    if (_stricmp(name, "capslock") == 0) return VK_CAPITAL;
    if (_stricmp(name, "numlock") == 0)  return VK_NUMLOCK;
    if (_stricmp(name, "scrolllock") == 0) return VK_SCROLL;
    if (_stricmp(name, "apps") == 0 || _stricmp(name, "menu") == 0) return VK_APPS;

    // 与 Python 版 _BUILTIN_KEYS 对齐的别名（Ctrl+Plus / Ctrl+Minus 等）
    if (_stricmp(name, "plus") == 0)  return VK_OEM_PLUS;
    if (_stricmp(name, "minus") == 0) return VK_OEM_MINUS;

    // 符号键
    if (strcmp(name, "-") == 0 || strcmp(name, "_") == 0) return VK_OEM_MINUS;
    if (strcmp(name, "=") == 0 || strcmp(name, "+") == 0) return VK_OEM_PLUS;
    if (strcmp(name, "[") == 0 || strcmp(name, "{") == 0) return VK_OEM_4;
    if (strcmp(name, "]") == 0 || strcmp(name, "}") == 0) return VK_OEM_6;
    if (strcmp(name, "\\") == 0 || strcmp(name, "|") == 0) return VK_OEM_5;
    if (strcmp(name, ";") == 0 || strcmp(name, ":") == 0) return VK_OEM_1;
    if (strcmp(name, "'") == 0 || strcmp(name, "\"") == 0) return VK_OEM_7;
    if (strcmp(name, ",") == 0 || strcmp(name, "<") == 0) return VK_OEM_COMMA;
    if (strcmp(name, ".") == 0 || strcmp(name, ">") == 0) return VK_OEM_PERIOD;
    if (strcmp(name, "/") == 0 || strcmp(name, "?") == 0) return VK_OEM_2;
    if (strcmp(name, "`") == 0 || strcmp(name, "~") == 0) return VK_OEM_3;

    return 0;
}

/// 该虚拟键是否为「扩展键」，需要 KEYEVENTF_EXTENDEDKEY。
/// 与 Python 版 adapter._EXTENDED_KEYS 保持一致。
static bool is_extended_key(WORD vk) {
    switch (vk) {
        case VK_LWIN: case VK_RWIN: case VK_APPS:
        case VK_PRIOR: case VK_NEXT: case VK_END: case VK_HOME:
        case VK_LEFT: case VK_UP: case VK_RIGHT: case VK_DOWN:
        case VK_INSERT: case VK_DELETE:
        case VK_RSHIFT: case VK_RCONTROL: case VK_RMENU:
        case VK_SNAPSHOT: case VK_NUMLOCK: case VK_DIVIDE:
            return true;
        default:
            return false;
    }
}

/// 是否为修饰键（用于判断主键是否需要自动补 Shift）
static bool is_modifier(WORD vk) {
    return vk == VK_CONTROL || vk == VK_MENU || vk == VK_SHIFT ||
           vk == VK_LWIN || vk == VK_RWIN;
}

// ── INPUT 构造 ────────────────────────────────────────────────────

/// 构造一个 scan-code 键盘事件。
static INPUT make_key_input(WORD vk, bool keydown) {
    DWORD flags = KEYEVENTF_SCANCODE;
    if (!keydown)          flags |= KEYEVENTF_KEYUP;
    if (is_extended_key(vk)) flags |= KEYEVENTF_EXTENDEDKEY;

    // 扩展键（Win/方向/Home/End/Delete…）需用 MAPVK_VK_TO_VSC_EX 才能拿到
    // 正确的双字节（E0 前缀）扫描码；否则会被识别成小键盘键位，组合行为异常。
    UINT map_type = is_extended_key(vk) ? MAPVK_VK_TO_VSC_EX : MAPVK_VK_TO_VSC;
    WORD scan = (WORD)MapVirtualKeyW(vk, map_type);

    INPUT input{};
    input.type       = INPUT_KEYBOARD;
    input.ki.wVk     = 0;      // scan-code 模式：wVk 必须为 0
    input.ki.wScan   = scan;
    input.ki.dwFlags = flags;
    return input;
}

/// 经 keybd_event 发送单个键的按下/释放。
/// 作为 SendInput 被系统拒绝（如 ERROR_INVALID_PARAMETER / UIPI 子会话）时的
/// 保底路径：keybd_event 走老式输入管线，对进程输入队列状态的校验更宽松。
static void legacy_key_event(WORD vk, bool keydown) {
    DWORD flags = 0;
    if (is_extended_key(vk)) flags |= KEYEVENTF_EXTENDEDKEY;
    if (!keydown)            flags |= KEYEVENTF_KEYUP;
    // keybd_event 用 wVk（虚拟键码）即可，扫描码由系统补全。
    keybd_event(vk, (BYTE)MapVirtualKeyW(vk, MAPVK_VK_TO_VSC), flags, 0);
}

// ── 公开 API ──────────────────────────────────────────────────────

/// 解析键位组合为 (修饰键序列, 主键, 是否需自动补 Shift)。
/// 供 send_hotkey / validate_hotkey 共用，保证「能校验通过的一定能执行」。
/// @return true 解析成功
static bool parse_hotkey(const char* key_combination,
                         std::vector<WORD>& mods,
                         WORD& main_vk,
                         bool& solo_upper) {
    mods.clear();
    main_vk = 0;
    solo_upper = false;
    if (!key_combination || !*key_combination) return false;

    // ── 切分 "Ctrl+Shift+C" ──
    // 末尾的 '+' 视为主键本身（'Ctrl++' → 主键 '+'），否则会被当成空片段丢弃。
    std::vector<std::string> parts;
    {
        const char* p = key_combination;
        const char* start = p;
        while (true) {
            if (*p == '+' || *p == '\0') {
                std::string part(start, p - start);
                while (!part.empty() && part.front() == ' ') part.erase(0, 1);
                while (!part.empty() && part.back() == ' ')  part.pop_back();
                if (!part.empty()) {
                    parts.push_back(part);
                } else if (*p == '+' && *(p + 1) == '\0') {
                    parts.push_back("+");   // 形如 "Ctrl++"
                }
                if (*p == '\0') break;
                start = p + 1;
            }
            p++;
        }
    }
    if (parts.empty()) return false;

    // ── 拆分修饰键 / 主键 ──
    // 不能简单取「最后一个」：按是否为修饰键判定更稳，
    // 且允许 "Numlock+-" 这类以非修饰键开头的组合。
    for (size_t i = 0; i < parts.size(); i++) {
        WORD vk = resolve_vk(parts[i].c_str());
        if (vk == 0) return false;               // 无法识别的键位
        if (is_modifier(vk) && i + 1 < parts.size()) {
            mods.push_back(vk);
        } else {
            main_vk = vk;                        // 最后一个非修饰键为主键
        }
    }
    if (main_vk == 0) {
        // 全是修饰键（如单按 "Win"）——把最后一个当主键
        if (mods.empty()) return false;
        main_vk = mods.back();
        mods.pop_back();
    }

    // ── 单字母大写且无修饰键时自动补 Shift ──
    // 与 Python 版一致：组合键里的大小写只表示物理键，不代表 Shift 状态。
    const std::string& last = parts.back();
    if (mods.empty() && last.size() == 1 && last[0] >= 'A' && last[0] <= 'Z')
        solo_upper = true;

    return true;
}

int validate_hotkey(const char* key_combination) {
    std::vector<WORD> mods;
    WORD main_vk = 0;
    bool solo_upper = false;
    return parse_hotkey(key_combination, mods, main_vk, solo_upper) ? 0 : -1;
}

int send_hotkey(const char* key_combination) {
    std::vector<WORD> mods;
    WORD main_vk = 0;
    bool solo_upper = false;
    if (!parse_hotkey(key_combination, mods, main_vk, solo_upper)) return -1;

    // ── 批量构造：按下修饰键 → 点主键 → 反序释放 ──
    std::vector<INPUT> seq;
    seq.reserve((mods.size() + 2) * 2);

    if (solo_upper) seq.push_back(make_key_input(VK_SHIFT, true));
    for (size_t i = 0; i < mods.size(); i++)
        seq.push_back(make_key_input(mods[i], true));

    seq.push_back(make_key_input(main_vk, true));
    seq.push_back(make_key_input(main_vk, false));

    for (size_t i = mods.size(); i > 0; i--)
        seq.push_back(make_key_input(mods[i - 1], false));
    if (solo_upper) seq.push_back(make_key_input(VK_SHIFT, false));

    // 一次性提交：原子性更好，也比逐键 SendInput 快
    UINT sent = SendInput((UINT)seq.size(), seq.data(), sizeof(INPUT));
    if (sent == seq.size())
        return 0;

    // ── 保底：SendInput 被环境拒绝（UIPI / 子会话 / 输入队列状态）时，
    //    退回 keybd_event 管线逐键发送。keybd_event 对调用进程的输入队列
    //    校验更宽松，在非交互式集成终端里往往仍能真正投递。
    if (solo_upper) legacy_key_event(VK_SHIFT, true);
    for (size_t i = 0; i < mods.size(); i++)
        legacy_key_event(mods[i], true);
    legacy_key_event(main_vk, true);
    legacy_key_event(main_vk, false);
    for (size_t i = mods.size(); i > 0; i--)
        legacy_key_event(mods[i - 1], false);
    if (solo_upper) legacy_key_event(VK_SHIFT, false);

    return 0;   // 保底路径总是视为已尽力投递
}

int send_unicode_char(const wchar_t* text) {
    if (!text || !*text) return -1;

    size_t len = wcslen(text);
    if (len > 512) len = 512; // safety cap

    std::vector<INPUT> inputs;
    inputs.reserve(len * 2);

    for (size_t i = 0; i < len; i++) {
        // keydown
        INPUT down{};
        down.type = INPUT_KEYBOARD;
        down.ki.wVk = 0;
        down.ki.wScan = text[i];
        down.ki.dwFlags = KEYEVENTF_UNICODE;
        inputs.push_back(down);

        // keyup
        INPUT up{};
        up.type = INPUT_KEYBOARD;
        up.ki.wVk = 0;
        up.ki.wScan = text[i];
        up.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;
        inputs.push_back(up);
    }

    UINT sent = SendInput((UINT)inputs.size(), inputs.data(), sizeof(INPUT));
    return (sent == inputs.size()) ? 0 : -1;
}

int type_via_clipboard(const wchar_t* text) {
    if (!text || !*text) return -1;

    size_t len = wcslen(text);
    size_t bytes = (len + 1) * sizeof(wchar_t);

    // 1. 写入剪贴板
    if (!OpenClipboard(nullptr)) {
        Sleep(50);
        if (!OpenClipboard(nullptr)) return -1;
    }
    EmptyClipboard();

    HGLOBAL hMem = GlobalAlloc(GMEM_MOVEABLE, bytes);
    if (!hMem) {
        CloseClipboard();
        return -1;
    }

    wchar_t* lock = (wchar_t*)GlobalLock(hMem);
    if (!lock) {
        GlobalFree(hMem);
        CloseClipboard();
        return -1;
    }
    memcpy(lock, text, bytes);
    GlobalUnlock(hMem);

    SetClipboardData(CF_UNICODETEXT, hMem);
    CloseClipboard();

    // 2. 发送 Ctrl+V（SendInput 优先，失败回退 keybd_event）
    INPUT seq[4] = {
        make_key_input(VK_CONTROL, true),
        make_key_input('V', true),
        make_key_input('V', false),
        make_key_input(VK_CONTROL, false),
    };
    if (SendInput(4, seq, sizeof(INPUT)) != 4) {
        legacy_key_event(VK_CONTROL, true);
        legacy_key_event('V', true);
        legacy_key_event('V', false);
        legacy_key_event(VK_CONTROL, false);
    }

    return 0;
}

void free_result(void* ptr) {
    if (ptr) CoTaskMemFree(ptr);
}
