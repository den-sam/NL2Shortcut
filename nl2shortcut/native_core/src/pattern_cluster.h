#pragma once

#include "export.h"

#ifdef __cplusplus
extern "C" {
#endif

/// 操作记录的聚类引擎。
///
/// 输入：records_json —— JSON 数组，每条记录形如：
///   {"timestamp":"2026-01-01T10:00:00", "app":"notepad",
///    "action_type":"shortcut", "action_detail":"Ctrl+C",
///    "duration_ms":80}
///
/// 算法（与 operation_memory.py::learn_patterns 等价）：
///   1. 按 timestamp 升序排序
///   2. 按 app 分组 + 相邻时间间隔 < gap_seconds 的连续记录归为一段 sequence
///   3. 对每段生成 canonical 签名（shortcut 走 canonical_key 归一）
///   4. 按签名聚类，统计 frequency
///   5. 过滤 frequency >= min_frequency
///
/// 输出：patterns_json —— JSON 数组，每个元素形如：
///   {"app":"notepad", "frequency":3, "avg_duration_ms":65,
///    "total_duration_ms":130,
///    "steps":[{"type":"shortcut","key":"Ctrl+A"}, ...],
///    "signature":"notepad|shortcut:Ctrl+A→shortcut:Ctrl+C→"}
///
/// 调用方需用 free_result() 释放返回的字符串。
/// 失败返回 nullptr。
///
/// @param records_json       操作记录 JSON 数组
/// @param gap_seconds        同一序列内相邻记录的最大时间间隔（默认 30）
/// @param min_sequence_length 序列最短长度（默认 1）
/// @param min_frequency      聚类为 pattern 的最小频次（默认 3）
NL2S_API const char* cluster_operations(
    const char* records_json,
    int gap_seconds,
    int min_sequence_length,
    int min_frequency
);

/// 将按键字符串规范化为唯一形式（语法归一 + 语义等价合并）。
/// 与 operation_memory.py::canonical_key 完全等价。
/// 调用方需用 free_result() 释放返回的字符串。
NL2S_API const char* canonical_key(const char* detail);

#ifdef __cplusplus
}
#endif
