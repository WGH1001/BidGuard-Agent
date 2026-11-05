# 任务卡 B1 · backend 数据层（database + 9 表 ORM + auth 完整化 + .env 扩充）

> 执行者零上下文进场：先读根目录 `INTERFACE.md`（§2 表规格 + §3 横切约定），再读本卡全文，然后动手。
> 蓝本项目：`E:\agent\Contract-Review-Agent\backend\`（`database.py`、`models.py`、`auth.py`、`utils/crypto.py`）——
> 继承其成熟逻辑，字段按 INTERFACE.md §2 裁剪，业务语义按本卡改写。

---

## 1. 任务背景

BidGuard-Agent 的 core 引擎层（F1~F7）已全部完成并通过 71 项 pytest。现在启动后端服务层第一步：
**数据库底座**。本卡只做数据层与鉴权层，**不写任何路由、不改 main.py**（路由是 B2 卡的事）。

## 2. 交付物（本卡共改/建 4 个文件 + 1 个测试文件）

| 文件 | 动作 | 要求 |
|------|------|------|
| `backend/database.py` | 新建 | SQLAlchemy engine / SessionLocal / Base / `get_db` 依赖。`DATABASE_URL` 读环境变量，缺省 `sqlite:///./data/bidguard.db`；sqlite 相对路径必须锚定项目根（`Path(__file__).resolve().parents[1]`），禁止跟随 CWD |
| `backend/models.py` | 新建 | 严格按 `INTERFACE.md §2` 建 9 张表：tenants / users / bid_check_tasks / bid_findings / bid_rule_configs / company_certificates / qa_sessions / audit_logs / user_model_configs。金额列 `Numeric(14,2)`；JSON 列用 `sa.JSON`；`bid_findings.task_id`、`qa_sessions.task_id` 对 tasks 级联删除 |
| `backend/auth.py` | 扩充 | 保留现有 `get_auth_secret()` 密钥守卫逻辑**一字不动**，向下追加完整鉴权层（以老项目 auth.py 为蓝本）：bcrypt 哈希与校验、HMAC-SHA256 令牌签发/校验（payload 含 uid/tenant/role/exp）、FastAPI 依赖 `get_current_user` / `require_admin`、`hash_password` / `verify_password`、审计留痕辅助函数。令牌过期、签名不符、用户禁用分别抛 401 |
| `.env.example` | 扩充 | 按 `HANDOVER.md §8.2` 清单扩为 14 变量（MODEL_ENV / ZHIPU_API_KEY / PRIVATE_API_KEY / AUTH_SECRET / SSO_SECRET / ADMIN_INITIAL_PASSWORD / DATABASE_URL / REDIS_URL / APP_PORT / BACKEND_INTERNAL_PORT / CORS_ORIGINS / LOG_LEVEL / SEED_DEMO_DATA / SHOW_DEMO_LOGIN），严禁填真实密钥 |
| `tests/test_backend_models.py` | 新建 | 见 §4 验收标准 |

## 3. 实现铁律（违反即打回）

1. **契约锁死**：表名、字段名、枚举值必须与 `INTERFACE.md §2` 逐字一致（后端 B2 卡和前端都按它消费）。
2. **密钥安全**：`user_model_configs.api_key_encrypted` 用 `backend/utils/crypto.py` 既有函数加解密（主密钥派生自 `get_auth_secret()`），本卡不重造加密；明文 key 严禁出现在日志、异常消息、repr 中。
3. **库模块静默**：只抛异常，禁止 print；测试外不留任何副作用写盘（建表只在 `Base.metadata.create_all` 被显式调用时发生）。
4. **生产守卫**：DEBUG 关闭且 AUTH_SECRET 未配置/为开发默认值时，启动与签发令牌必须抛 RuntimeError（现有守卫已实现，勿删）。
5. **跨平台**：所有文件读写显式 `encoding='utf-8'`；PowerShell 5.1 不支持 `&&`，命令逐行执行。
6. **不动别人的东西**：不修改 core/ 任何文件、不修改 backend/main.py、不修改 backend/utils/crypto.py、不建 routers 下任何路由文件。

## 4. 验收标准（一卡一验，全绿入库）

1. 新增 `tests/test_backend_models.py`，基于 `sqlite:///:memory:`（或 tmp_path 文件库）覆盖：
   - 9 张表 `create_all` 成功、缺列即建表失败用例；
   - users 密码哈希 roundtrip（hash→verify 通过 / 错误密码拒绝）；
   - HMAC 令牌 roundtrip（签发→校验通过；篡改 payload / 过期令牌 / 错误密钥 各 1 拒绝用例）；
   - 租户隔离：tenant A 建的 task，用 tenant B 的过滤条件查不到；
   - 级联删除：删 task 后其 findings 同步消失；
   - api_key 加密 roundtrip（crypto.py 加密落库→读出解密还原）；
   - **断言必须数数（如 `assert len(rows) == 1`），严禁空循环假绿**。
2. 全量 `pytest tests/ -q` 零回归（当前基线 71 项全绿，本卡完成后 ≥ 71 + 新增数）。
3. `git status` 干净入库，commit message 首行：`feat(B1): backend data layer - 9-table ORM + auth full version`。

## 5. 本卡涉及文件清单（实现前先逐个读取原文，基于最新版改）

```text
INTERFACE.md                                  # 契约：§2 表规格 / §3.5 环境变量（必读）
backend/auth.py                               # 在其上扩充
backend/utils/crypto.py                       # 只读复用，禁止修改
.env.example                                  # 扩充
requirements.txt                              # 若蓝本 auth/database 需要额外依赖（如 psycopg2-binary），取消注释或追加并注明用途
E:\agent\Contract-Review-Agent\backend\auth.py        # 蓝本（只读参考）
E:\agent\Contract-Review-Agent\backend\database.py    # 蓝本（只读参考）
E:\agent\Contract-Review-Agent\backend\models.py      # 蓝本（只读参考）
```
