# @lumen/web — Agent Workspace

Stage 9.2 脚手架。Vite + React 18 + TypeScript + Tailwind CSS + shadcn/ui + React Router + TanStack Query + Axios。

## 项目简介

Lumen AI Support Agent 的坐席工作台前端。负责登录、收件箱、会话详情、知识库与租户设置等页面(后续任务逐步填充,9.3-9.7 实现页面,9.2 仅搭建路由与基础组件)。

## 开发命令

```bash
pnpm install            # 安装依赖
pnpm dev                # 本地开发服务,默认 http://localhost:5173
pnpm build              # 类型检查 + 生产构建到 dist/
pnpm preview            # 预览构建产物
pnpm lint               # ESLint (ts + react-hooks + react-refresh)
pnpm type-check         # tsc --noEmit 严格类型检查
```

## 环境变量

复制 `.env.example` 到 `.env.local`,值以 `VITE_` 开头才会暴露给客户端:

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://localhost:8000` | 后端 FastAPI 地址,生产环境指向正式域名 |

## 技术栈

- **构建**: Vite 5 + `@vitejs/plugin-react`
- **语言**: TypeScript 5,严格模式(`strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`)
- **UI**: Tailwind CSS 3.4 + shadcn/ui(基础 primitive: Button / Input / Card)
- **路由**: React Router v6(`BrowserRouter`, `Routes`, `NavLink`)
- **数据层**: TanStack Query v5(`QueryClient` + Devtools,默认 `staleTime: 30s`)
- **HTTP**: Axios(请求拦截器附加 `Authorization: Bearer <jwt>`,响应拦截器 401 → 跳转登录)
- **图标**: lucide-react

## 目录结构

```
apps/web/
├── index.html              # Vite 入口 HTML
├── vite.config.ts          # Vite 配置 + @ 别名 + /api dev proxy
├── tailwind.config.ts      # Tailwind 配置(shadcn 设计 token)
├── postcss.config.js       # PostCSS 配置
├── components.json         # shadcn/ui 配置
├── tsconfig.json           # 引用 app/node 配置
├── tsconfig.app.json       # 应用代码严格类型配置
├── tsconfig.node.json      # Node 端(vite.config 等)配置
├── .eslintrc.cjs           # ESLint 配置
├── .env.example            # 环境变量样例
├── .gitignore
└── src/
    ├── main.tsx            # React 入口(QueryClientProvider + BrowserRouter)
    ├── App.tsx             # 路由表(AuthGuard + AppShell)
    ├── index.css           # Tailwind layers + shadcn CSS 变量
    ├── vite-env.d.ts       # Vite 客户端类型声明
    ├── lib/
    │   ├── utils.ts        # cn(clsx + tailwind-merge)
    │   ├── api-client.ts   # Axios 实例 + JWT 拦截器
    │   └── query-client.ts # TanStack QueryClient
    ├── components/
    │   ├── ui/             # shadcn 基础组件(button / input / card)
    │   ├── layout/
    │   │   └── app-shell.tsx   # Top bar + Side nav + <Outlet />
    │   └── auth/
    │       └── auth-guard.tsx  # localStorage.jwt 守卫
    └── pages/              # 路由占位(9.3-9.7 填充)
        ├── login.tsx
        ├── inbox.tsx
        ├── inbox-detail.tsx
        ├── kb.tsx
        └── settings.tsx
```

## 路由表

| Path | 组件 | 守卫 |
| --- | --- | --- |
| `/login` | `<LoginPage />` | — |
| `/inbox` | `<InboxPage />` | AuthGuard |
| `/inbox/:id` | `<InboxDetailPage />` | AuthGuard |
| `/kb` | `<KbPage />` | AuthGuard |
| `/settings` | `<SettingsPage />` | AuthGuard |
| `/`, `*` | 重定向到 `/inbox` | — |

## 设计 Token

`src/index.css` 定义 shadcn 风格 HSL CSS 变量(背景、前景、主色、次色、强调、危险、边框、输入、环、卡片),通过 Tailwind `theme.extend.colors` 暴露为工具类(如 `bg-background`, `text-foreground`, `border-border`)。`dark` 类挂在 `<html>` 上可一键切换暗色(具体接入留到后续任务)。

## 开发代理

`vite.config.ts` 将 `/api/*` 反代到 `http://localhost:8000`,开发期无需 CORS 预检即可访问 FastAPI 后端。生产环境由 Nginx/网关层处理。

## 鉴权约定

- JWT 存放在 `localStorage['lumen.jwt']`(`@/lib/api-client` 导出常量 `JWT_STORAGE_KEY`)。
- `AuthGuard` 缺失 token 时跳 `/login`;Axios 拦截器遇到 401 自动清 token 并跳转。
- 登录页(9.3)负责写入 token;AppShell 的"退出登录"按钮仅清 token + 跳登录。

## 关联

- 设计文档:`docs/superpowers/specs/2026-09-10-ai-customer-service-design.md`
- 实施计划:`docs/superpowers/plans/2026-09-10-ai-customer-m1.md`(Stage 9)
