| 窗口 | runtime 去重 | 下游幂等 | 次数 | 标记命中 | 恢复成功 | 日志一致 | 出现重复副作用 | 单键最大效果数 | 与预期一致 |
|---|---|---|---|---|---|---|---|---|---|
| post_record_pre_commit | off | off | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_record_pre_commit | off | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_record_pre_commit | on | off | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_record_pre_commit | on | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_tool_effect_pre_record | off | off | 5 | 5/5 | 5/5 | 5/5 | 5/5 | 2 | as-predicted |
| post_tool_effect_pre_record | off | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_tool_effect_pre_record | on | off | 5 | 5/5 | 5/5 | 5/5 | 5/5 | 2 | as-predicted |
| post_tool_effect_pre_record | on | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| pre_tool_exec | off | off | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| pre_tool_exec | off | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| pre_tool_exec | on | off | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| pre_tool_exec | on | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
