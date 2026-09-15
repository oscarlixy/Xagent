# 私密 X List OAuth 设计

## 目标

允许唯一操作者通过 X OAuth 2.0 Authorization Code + PKCE 授权其账户，使应用可以只读同步私密 X List；访问令牌与刷新令牌不得出现在浏览器代码、日志、API 响应或 Git 中。

## 架构

浏览器访问前端的 `GET /api/x/authorize`。前端生成 PKCE verifier、challenge 与随机 state，把带 HMAC 的短期状态 Cookie 设为 HttpOnly、SameSite=Lax，然后重定向到 X 授权页。X 回调 `GET /api/x/callback` 验证 state 和 Cookie，向私有 FastAPI `POST /api/x/oauth/callback` 传送 code/verifier；后端交换令牌并加密持久化。

调度器/官方 X 源适配器只向后端的令牌服务取得有效 access token。令牌到期前 60 秒，服务用 refresh token 进行一次刷新并原子更新数据库。若刷新失败，保留已有记录、返回稳定错误码、暂停该次同步，不删除原始帖子。

## 配置

前端服务器环境：`X_CLIENT_ID`、`X_OAUTH_REDIRECT_URI`、`X_OAUTH_STATE_SECRET`、`BACKEND_INTERNAL_URL`、`INTERNAL_API_TOKEN`。后端环境：`X_CLIENT_ID`、`X_OAUTH_REDIRECT_URI`、`X_TOKEN_ENCRYPTION_KEY`。所有密钥至少 32 个随机字节；仅 `X_CLIENT_ID` 可视为非秘密，但不暴露给客户端包。

固定 OAuth scopes：`tweet.read users.read list.read offline.access`。默认 authorize URL 为 `https://x.com/i/oauth2/authorize`，token URL 为 `https://api.x.com/2/oauth2/token`。测试通过注入 URL/HTTP client 模拟，绝不触及 X。

## 持久化与安全

新增唯一 `oauth_credentials(provider)` 表，只存 Fernet 加密后的 access token/refresh token、到期时间、scope 与时间戳。`X_TOKEN_ENCRYPTION_KEY` 无效或缺失时，应用拒绝 OAuth 回调与私密读取。任何 API/status 响应不得含 token、code、verifier、refresh 状态细节或上游错误正文。

回调是唯一不要求 Basic Auth 的前端路径；它必须有有效、未过期、签名正确的 state Cookie。所有其他 `/api/x/*` 路径仍走操作者 Basic Auth。回调结束后删除 state Cookie，并只重定向到固定本地路径。

## 验收

- 回调交换将令牌加密保存；错误响应与日志不含秘密。
- state 篡改、缺失、过期及授权拒绝均不会交换令牌。
- 到期令牌恰好刷新一次；失败不覆盖旧记录。
- 适配器用 OAuth access token 调用私密 List 端点，并解析正常/逐项错误响应。
- 客户端构建扫描不含 `INTERNAL_API_TOKEN`、`X_TOKEN_ENCRYPTION_KEY`、OAuth code/verifier/token。
