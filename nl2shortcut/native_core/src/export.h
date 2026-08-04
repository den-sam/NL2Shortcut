#pragma once

/// DLL 导出宏。
///
/// 说明：`extern "C"` 只是禁用 C++ name mangling，**不会**把符号放进 DLL 导出表。
/// MSVC 下必须再加 `__declspec(dllexport)`，否则 ctypes 按名字找不到函数
/// （表现为 DLL 能加载、但每个 getattr 都抛 AttributeError）。
#ifdef _WIN32
#  define NL2S_API __declspec(dllexport)
#else
#  define NL2S_API __attribute__((visibility("default")))
#endif
