/// UIA 树采集引擎 —— C++ COM 直调，替代 Python uiautomation。
/// 采集 → JSON 序列化 → 返回给 Python ctypes，单次跨边界。

#include "uia.h"
#include "inject.h"   // for free_result
#include <Windows.h>
#include <UIAutomationClient.h>
#include <UIAutomation.h>
#include <comdef.h>        // _bstr_t
#include <string>
#include <vector>
#include <chrono>

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")

// ── 交互控件类型 ID ────────────────────────────────────────────────

static const int INTERACTIVE_IDS[] = {
    UIA_ButtonControlTypeId, UIA_EditControlTypeId, UIA_HyperlinkControlTypeId,
    UIA_ComboBoxControlTypeId, UIA_ListControlTypeId, UIA_ListItemControlTypeId,
    UIA_TreeControlTypeId, UIA_TreeItemControlTypeId, UIA_MenuControlTypeId,
    UIA_MenuItemControlTypeId, UIA_TabControlTypeId, UIA_TabItemControlTypeId,
    UIA_CheckBoxControlTypeId, UIA_RadioButtonControlTypeId, UIA_SliderControlTypeId,
    UIA_SpinnerControlTypeId, UIA_SplitButtonControlTypeId, UIA_GroupControlTypeId,
    UIA_ToolBarControlTypeId, UIA_MenuBarControlTypeId, UIA_DataGridControlTypeId,
    UIA_DataItemControlTypeId, UIA_ProgressBarControlTypeId, UIA_ScrollBarControlTypeId,
    UIA_StatusBarControlTypeId, UIA_TitleBarControlTypeId, UIA_ToolTipControlTypeId,
    UIA_PaneControlTypeId, UIA_HeaderControlTypeId, UIA_HeaderItemControlTypeId,
    UIA_ThumbControlTypeId, UIA_CalendarControlTypeId,
};
static const int INTERACTIVE_COUNT = sizeof(INTERACTIVE_IDS) / sizeof(INTERACTIVE_IDS[0]);

static bool is_interactive(int ct) {
    for (int i = 0; i < INTERACTIVE_COUNT; i++)
        if (INTERACTIVE_IDS[i] == ct) return true;
    return false;
}

static const char* control_type_name(int ct) {
    switch (ct) {
        case UIA_ButtonControlTypeId:      return "ButtonControl";
        case UIA_EditControlTypeId:        return "EditControl";
        case UIA_HyperlinkControlTypeId:   return "HyperlinkControl";
        case UIA_ComboBoxControlTypeId:    return "ComboBoxControl";
        case UIA_ListControlTypeId:        return "ListControl";
        case UIA_ListItemControlTypeId:    return "ListItemControl";
        case UIA_TreeControlTypeId:        return "TreeControl";
        case UIA_TreeItemControlTypeId:    return "TreeItemControl";
        case UIA_MenuControlTypeId:        return "MenuControl";
        case UIA_MenuItemControlTypeId:    return "MenuItemControl";
        case UIA_TabControlTypeId:         return "TabControl";
        case UIA_TabItemControlTypeId:     return "TabItemControl";
        case UIA_CheckBoxControlTypeId:    return "CheckBoxControl";
        case UIA_RadioButtonControlTypeId: return "RadioButtonControl";
        case UIA_SliderControlTypeId:      return "SliderControl";
        case UIA_SpinnerControlTypeId:     return "SpinnerControl";
        case UIA_SplitButtonControlTypeId: return "SplitButtonControl";
        case UIA_GroupControlTypeId:       return "GroupControl";
        case UIA_ToolBarControlTypeId:     return "ToolBarControl";
        case UIA_MenuBarControlTypeId:     return "MenuBarControl";
        case UIA_DataGridControlTypeId:    return "DataGridControl";
        case UIA_DataItemControlTypeId:    return "DataItemControl";
        case UIA_ProgressBarControlTypeId: return "ProgressBarControl";
        case UIA_ScrollBarControlTypeId:   return "ScrollBarControl";
        case UIA_StatusBarControlTypeId:   return "StatusBarControl";
        case UIA_TitleBarControlTypeId:    return "TitleBarControl";
        case UIA_ToolTipControlTypeId:     return "ToolTipControl";
        case UIA_PaneControlTypeId:        return "PaneControl";
        case UIA_HeaderControlTypeId:      return "HeaderControl";
        case UIA_HeaderItemControlTypeId:  return "HeaderItemControl";
        case UIA_ThumbControlTypeId:       return "ThumbControl";
        case UIA_CalendarControlTypeId:    return "CalendarControl";
        case UIA_WindowControlTypeId:      return "WindowControl";
        case UIA_DocumentControlTypeId:    return "DocumentControl";
        default:                           return "UnknownControl";
    }
}

static const char* control_role(int ct) {
    switch (ct) {
        case UIA_ButtonControlTypeId:      return "button";
        case UIA_EditControlTypeId:        return "textbox";
        case UIA_HyperlinkControlTypeId:   return "link";
        case UIA_ComboBoxControlTypeId:    return "combobox";
        case UIA_ListControlTypeId:        return "list";
        case UIA_ListItemControlTypeId:    return "listitem";
        case UIA_TreeControlTypeId:        return "tree";
        case UIA_TreeItemControlTypeId:    return "treeitem";
        case UIA_MenuControlTypeId:        return "menu";
        case UIA_MenuItemControlTypeId:    return "menuitem";
        case UIA_TabControlTypeId:         return "tab";
        case UIA_TabItemControlTypeId:     return "tabitem";
        case UIA_CheckBoxControlTypeId:    return "checkbox";
        case UIA_RadioButtonControlTypeId: return "radio";
        case UIA_SliderControlTypeId:      return "slider";
        case UIA_SpinnerControlTypeId:     return "spinner";
        case UIA_SplitButtonControlTypeId: return "splitbutton";
        case UIA_GroupControlTypeId:       return "group";
        case UIA_ToolBarControlTypeId:     return "toolbar";
        case UIA_MenuBarControlTypeId:     return "menubar";
        case UIA_DataGridControlTypeId:    return "datagrid";
        case UIA_DataItemControlTypeId:    return "dataitem";
        case UIA_ProgressBarControlTypeId: return "progressbar";
        case UIA_ScrollBarControlTypeId:   return "scrollbar";
        case UIA_StatusBarControlTypeId:   return "statusbar";
        case UIA_TitleBarControlTypeId:    return "titlebar";
        case UIA_ToolTipControlTypeId:     return "tooltip";
        case UIA_PaneControlTypeId:        return "pane";
        case UIA_HeaderControlTypeId:      return "header";
        case UIA_HeaderItemControlTypeId:  return "headeritem";
        case UIA_ThumbControlTypeId:       return "thumb";
        case UIA_CalendarControlTypeId:    return "calendar";
        case UIA_WindowControlTypeId:      return "window";
        case UIA_DocumentControlTypeId:    return "document";
        default:                           return "container";
    }
}

// ── 安全属性读取 ──────────────────────────────────────────────────

static std::string safe_bstr(BSTR bstr) {
    if (!bstr) return "";
    // 显式走一次 UTF-16 → UTF-8，避免 _bstr_t 隐式多重转换（C4927），
    // 同时保证中文等非 ASCII 字符按 UTF-8 正确编码（_bstr_t 走的是 ANSI 码页）。
    int len = WideCharToMultiByte(CP_UTF8, 0, bstr, -1, nullptr, 0, nullptr, nullptr);
    if (len <= 0) return "";
    std::vector<char> buf(len);
    WideCharToMultiByte(CP_UTF8, 0, bstr, -1, buf.data(), len, nullptr, nullptr);
    return std::string(buf.data());
}

static std::string safe_name(IUIAutomationElement* el) {
    BSTR b = nullptr;
    if (SUCCEEDED(el->get_CurrentName(&b)) && b)
        return safe_bstr(b);
    return "";
}

static int safe_ct(IUIAutomationElement* el) {
    CONTROLTYPEID ct{};
    if (SUCCEEDED(el->get_CurrentControlType(&ct)))
        return (int)ct;
    return 0;
}

static bool safe_bool_prop(IUIAutomationElement* el, HRESULT (STDMETHODCALLTYPE IUIAutomationElement::*getter)(BOOL*)) {
    BOOL v = FALSE;
    if (SUCCEEDED((el->*getter)(&v))) return v != FALSE;
    return false;
}

static RECT safe_rect(IUIAutomationElement* el) {
    RECT r{};
    el->get_CurrentBoundingRectangle(&r);
    return r;
}

static std::string safe_accel(IUIAutomationElement* el) {
    BSTR b = nullptr;
    if (SUCCEEDED(el->get_CurrentAcceleratorKey(&b)) && b)
        return safe_bstr(b);
    return "";
}

static std::string get_value_text(IUIAutomationElement* el, int ct) {
    if (ct != UIA_EditControlTypeId && ct != UIA_ComboBoxControlTypeId)
        return "";
    IUIAutomationValuePattern* vp = nullptr;
    if (SUCCEEDED(el->GetCurrentPatternAs(UIA_ValuePatternId, IID_PPV_ARGS(&vp))) && vp) {
        BSTR b = nullptr;
        if (SUCCEEDED(vp->get_CurrentValue(&b)) && b) {
            std::string s = safe_bstr(b);
            vp->Release();
            return s;
        }
        vp->Release();
    }
    return "";
}

static std::string get_patterns_str(IUIAutomationElement* el) {
    std::string result;
    static const struct { PATTERNID id; const char* name; } pats[] = {
        {UIA_InvokePatternId, "invoke"}, {UIA_ValuePatternId, "value"},
        {UIA_TogglePatternId, "toggle"}, {UIA_SelectionPatternId, "select"},
        {UIA_ScrollPatternId, "scroll"}, {UIA_ExpandCollapsePatternId, "expand_collapse"},
        {UIA_TextPatternId, "text"},
    };
    bool first = true;
    for (auto& p : pats) {
        IUnknown* unk = nullptr;
        if (SUCCEEDED(el->GetCurrentPatternAs(p.id, IID_PPV_ARGS(&unk))) && unk) {
            if (!first) result += ",";
            result += "\"";
            result += p.name;
            result += "\"";
            first = false;
            unk->Release();
        }
    }
    return "[" + result + "]";
}

// ── JSON 转义 ──────────────────────────────────────────────────────

static void json_escape(std::string& out, const std::string& s) {
    out += '"';
    for (char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:   out += c;
        }
    }
    out += '"';
}

// ── 树遍历 → JSON ─────────────────────────────────────────────────

static void build_node_json(IUIAutomation* uia, IUIAutomationElement* el,
                            int depth, int max_depth, int& count, int max_nodes,
                            std::string& out, bool* found_focus = nullptr)
{
    if (depth > max_depth || count >= max_nodes) return;

    int ct = safe_ct(el);
    bool is_ce = safe_bool_prop(el, &IUIAutomationElement::get_CurrentIsControlElement);

    // 穿透非 ControlElement (保留根节点)
    if (!is_ce && depth > 0) {
        IUIAutomationCondition* cond = nullptr;
        if (FAILED(uia->CreateTrueCondition(&cond)) || !cond) return;
        IUIAutomationElementArray* arr = nullptr;
        if (SUCCEEDED(el->FindAll(TreeScope_Children, cond, &arr)) && arr) {
            int child_count = 0;
            arr->get_Length(&child_count);
            for (int i = 0; i < child_count; i++) {
                IUIAutomationElement* child = nullptr;
                if (SUCCEEDED(arr->GetElement(i, &child)) && child) {
                    build_node_json(uia, child, depth + 1, max_depth, count, max_nodes, out, found_focus);
                    child->Release();
                }
            }
            arr->Release();
        }
        cond->Release();
        return;
    }

    // 跳过非交互控件 (保留根节点和 Document)
    if (depth > 0 && !is_interactive(ct) && ct != UIA_DocumentControlTypeId) {
        IUIAutomationCondition* cond = nullptr;
        if (FAILED(uia->CreateTrueCondition(&cond)) || !cond) return;
        IUIAutomationElementArray* arr = nullptr;
        if (SUCCEEDED(el->FindAll(TreeScope_Children, cond, &arr)) && arr) {
            int child_count = 0;
            arr->get_Length(&child_count);
            for (int i = 0; i < child_count; i++) {
                IUIAutomationElement* child = nullptr;
                if (SUCCEEDED(arr->GetElement(i, &child)) && child) {
                    build_node_json(uia, child, depth + 1, max_depth, count, max_nodes, out, found_focus);
                    child->Release();
                }
            }
            arr->Release();
        }
        cond->Release();
        return;
    }

    count++;
    std::string name = safe_name(el);
    RECT rect = safe_rect(el);
    bool focused = safe_bool_prop(el, &IUIAutomationElement::get_CurrentHasKeyboardFocus);
    bool enabled = safe_bool_prop(el, &IUIAutomationElement::get_CurrentIsEnabled);
    const char* state_str = focused ? "focused" : (!enabled ? "disabled" : "enabled");

    if (focused && found_focus) *found_focus = true;

    // 写节点 JSON
    out += "{";
    out += "\"name\":"; json_escape(out, name); out += ",";
    out += "\"control_type\":\""; out += control_type_name(ct); out += "\",";
    out += "\"role\":\""; out += control_role(ct); out += "\",";
    out += "\"value\":"; json_escape(out, get_value_text(el, ct)); out += ",";
    out += "\"state\":\""; out += state_str; out += "\",";
    out += "\"x\":"; out += std::to_string(rect.left); out += ",";
    out += "\"y\":"; out += std::to_string(rect.top); out += ",";
    out += "\"width\":"; out += std::to_string(rect.right - rect.left); out += ",";
    out += "\"height\":"; out += std::to_string(rect.bottom - rect.top); out += ",";
    out += "\"patterns\":"; out += get_patterns_str(el); out += ",";
    out += "\"keyboard_shortcut\":"; json_escape(out, safe_accel(el)); out += ",";

    // Children
    out += "\"children\":[";
    IUIAutomationCondition* cond = nullptr;
    if (SUCCEEDED(uia->CreateTrueCondition(&cond)) && cond) {
        IUIAutomationElementArray* arr = nullptr;
        if (SUCCEEDED(el->FindAll(TreeScope_Children, cond, &arr)) && arr) {
            int child_count = 0;
            arr->get_Length(&child_count);
            bool first_child = true;
            for (int i = 0; i < child_count; i++) {
                IUIAutomationElement* child = nullptr;
                if (SUCCEEDED(arr->GetElement(i, &child)) && child) {
                    if (!first_child) out += ",";
                    first_child = false;
                    build_node_json(uia, child, depth + 1, max_depth, count, max_nodes, out, found_focus);
                    child->Release();
                }
            }
            arr->Release();
        }
        cond->Release();
    }
    out += "]";
    out += "}";
}

// ── 进程名查找 ────────────────────────────────────────────────────

#include <TlHelp32.h>
#pragma comment(lib, "kernel32.lib")

static std::string get_process_name(DWORD pid) {
    if (pid == 0) return "";
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return "";
    PROCESSENTRY32W pe{ sizeof(pe) };
    std::string result;
    if (Process32FirstW(snap, &pe)) {
        do {
            if (pe.th32ProcessID == pid) {
                // 宽字符 → UTF-8
                int len = WideCharToMultiByte(CP_UTF8, 0, pe.szExeFile, -1, nullptr, 0, nullptr, nullptr);
                if (len > 0) {
                    std::vector<char> buf(len);
                    WideCharToMultiByte(CP_UTF8, 0, pe.szExeFile, -1, buf.data(), len, nullptr, nullptr);
                    result = buf.data();
                }
                break;
            }
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    return result;
}

static std::string friendly_app_name(const std::string& process_name) {
    std::string pn = process_name;
    for (auto& c : pn) c = (char)tolower((unsigned char)c);

    static const struct { const char* key; const char* friendly; } map[] = {
        {"code.exe", "vscode"}, {"devenv.exe", "visual_studio"},
        {"chrome.exe", "chrome"}, {"firefox.exe", "firefox"},
        {"msedge.exe", "edge"}, {"notepad.exe", "notepad"},
        {"notepad++.exe", "notepad++"}, {"explorer.exe", "explorer"},
        {"cmd.exe", "terminal"}, {"powershell.exe", "terminal"},
        {"windowsterminal.exe", "terminal"}, {"outlook.exe", "outlook"},
        {"excel.exe", "excel"}, {"winword.exe", "word"},
        {"powerpnt.exe", "powerpoint"}, {"teams.exe", "teams"},
        {"slack.exe", "slack"}, {"discord.exe", "discord"},
        {"spotify.exe", "spotify"},
    };
    for (auto& m : map) {
        if (pn.find(m.key) != std::string::npos)
            return m.friendly;
    }
    // fallback: 去掉 .exe
    if (pn.size() > 4 && pn.substr(pn.size() - 4) == ".exe")
        pn = pn.substr(0, pn.size() - 4);
    return pn;
}

// ── 公开入口 ──────────────────────────────────────────────────────

const char* uia_snapshot(int max_depth, int max_nodes) {
    auto t0 = std::chrono::high_resolution_clock::now();

    // COM 初始化（幂等）：仅在首次真正初始化，且**绝不调用 CoUninitialize**。
    // 原因：调用方（Python 进程 / Qt GUI 主线程）往往已建立 COM 套间，
    // 若我们每次 uia_snapshot 结束都 CoUninitialize()，会关闭整个线程的
    // COM 套间，导致下次调用或调用方的 COM 操作在已销毁的套间上运行 → 堆损坏
    // （典型表现：连续调用 uia_snapshot 第二次即 0xC0000374 崩溃）。
    // 进程退出时由 OS 统一回收 COM，无需手动反初始化。
    static bool g_com_ready = false;
    if (!g_com_ready) {
        HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
        // S_OK: 本次初始化成功；S_FALSE: 已初始化（同套间）；
        // RPC_E_CHANGED_MODE: 调用方已用不同套间模式初始化——COM 仍可用。
        if (hr == S_OK || hr == S_FALSE || hr == RPC_E_CHANGED_MODE)
            g_com_ready = true;
    }

    IUIAutomation* uia = nullptr;
    HRESULT hr = CoCreateInstance(__uuidof(CUIAutomation), nullptr, CLSCTX_ALL,
                          IID_PPV_ARGS(&uia));
    if (FAILED(hr) || !uia) {
        return nullptr;
    }

    // 获取焦点元素
    IUIAutomationElement* focus_el = nullptr;
    hr = uia->GetFocusedElement(&focus_el);
    if (FAILED(hr) || !focus_el) {
        uia->Release();
        return nullptr;
    }

    // 窗口信息
    std::string window_title = safe_name(focus_el);
    int pid = 0;
    focus_el->get_CurrentProcessId(&pid);
    std::string process_name = get_process_name((DWORD)pid);
    std::string app_name = friendly_app_name(process_name);

    // 递归遍历 → JSON
    int node_count = 0;
    bool found_focus = false;
    std::string tree_json;
    build_node_json(uia, focus_el, 0, max_depth, node_count, max_nodes, tree_json, &found_focus);

    focus_el->Release();
    uia->Release();

    // 计算耗时
    auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::high_resolution_clock::now() - t0).count() / 1000.0;

    // 构造完整 JSON
    std::string json;
    json += "{";
    json += "\"app_name\":\""; json += app_name; json += "\",";
    json += "\"window_title\":"; json_escape(json, window_title); json += ",";
    json += "\"process_name\":\""; json += process_name; json += "\",";
    json += "\"node_count\":"; json += std::to_string(node_count); json += ",";
    json += "\"root\":"; json += tree_json; json += ",";
    json += "\"focus\":{\"name\":\"\",\"control_type\":\"\",\"role\":\"\",\"state\":\"\"},";
    json += "\"elapsed_ms\":"; json += std::to_string(elapsed);
    json += "}";

    // CoTaskMemAlloc 分配（跨 DLL 安全）
    size_t len = json.size() + 1;
    char* buf = (char*)CoTaskMemAlloc(len);
    if (buf) memcpy(buf, json.c_str(), len);
    return buf;
}
