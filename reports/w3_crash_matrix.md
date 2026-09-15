| 格 | 次数 | 注入/阶段 | 日志一致 | 效果数 | 重复运行 | 单键最大 | unknown 行 | 探针对账 | 预期重复 | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|
| (tamper) outbox=1 idem=0 probe=1 tamper=1 | 5 | 5/5 | 5/5 | 0 | 0/5 | 0 | 0 | 0 | False | as-predicted |
| after_resume outbox=0 idem=0 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 0 | False | as-predicted |
| after_resume outbox=1 idem=0 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 0 | False | as-predicted |
| post_approval_pre_exec outbox=0 idem=0 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 0 | False | as-predicted |
| post_approval_pre_exec outbox=1 idem=0 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 0 | False | as-predicted |
| post_record_pre_commit outbox=0 idem=0 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 0 | False | as-predicted |
| post_record_pre_commit outbox=1 idem=0 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 0 | False | as-predicted |
| post_tool_effect_pre_record outbox=0 idem=0 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 2 | 5/5 | 2 | 0 | 0 | True | as-predicted |
| post_tool_effect_pre_record outbox=0 idem=1 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 0 | False | as-predicted |
| post_tool_effect_pre_record outbox=1 idem=0 probe=0 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 1 | 0 | False | as-predicted |
| post_tool_effect_pre_record outbox=1 idem=0 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 1 | False | as-predicted |
| post_tool_effect_pre_record outbox=1 idem=1 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 0 | False | as-predicted |
| pre_tool_exec outbox=0 idem=0 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 0 | False | as-predicted |
| pre_tool_exec outbox=1 idem=0 probe=1 tamper=0 | 5 | 5/5 | 5/5 | 1 | 0/5 | 1 | 0 | 0 | False | as-predicted |
