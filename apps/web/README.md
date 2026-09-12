# @lumen/web — Agent Workspace

Stage 9.1 脚手架。Vite + React 18 + TypeScript,严格模式,pnpm。

## 项目简介

Lumen AI Support Agent 的坐席工作台前端。负责登录、收件箱、会话详情、知识库与租户设置等页面(后续任务逐步填充,本任务仅搭建空壳)。

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

复制到 `.env.local`,值以 `VITE_` 开头才会暴露给客户端:

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://localhost:8000` | 后端 FastAPI 地址 |

## 目录结构

```
apps/web/
├── index.html              # Vite 入口 HTML
├── vite.config.ts          # Vite 配置 + @ 别名
├── tsconfig.json           # 引用 app/node 配置
├── tsconfig.app.json       # 应用代码严格类型配置
├── tsconfig.node.json      # Node 端(vite.config 等)配置
├── .eslintrc.cjs           # ESLint 配置
├── .gitignore
└── src/
    ├── main.tsx            # React 入口
    ├── App.tsx             # 占位 Landing 页
    ├── index.css           # 全局样式 + .app-shell 规则
    └── vite-env.d.ts       # Vite 客户端类型声明
```

## 关联

- 设计文档:`docs/superpowers/specs/2026-09-10-ai-customer-service-design.md`
- 实施计划:`docs/superpowers/plans/2026-09-10-ai-customer-m1.md`(Stage 9)
