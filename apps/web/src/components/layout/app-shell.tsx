import { Outlet, NavLink, useNavigate } from 'react-router-dom';
import { Inbox, BookOpen, Settings, LogOut } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { clearAuthToken } from '@/lib/api-client';
import { cn } from '@/lib/utils';

interface NavItem {
  to: string;
  label: string;
  icon: typeof Inbox;
}

const navItems: NavItem[] = [
  { to: '/inbox', label: '收件箱', icon: Inbox },
  { to: '/kb', label: '知识库', icon: BookOpen },
  { to: '/settings', label: '设置', icon: Settings },
];

export function AppShell(): JSX.Element {
  const navigate = useNavigate();

  const handleLogout = (): void => {
    clearAuthToken();
    navigate('/login', { replace: true });
  };

  return (
    <div className="flex h-screen w-full flex-col bg-background text-foreground">
      <header className="flex h-14 shrink-0 items-center justify-between border-b px-6">
        <div className="flex items-center gap-3">
          <span className="text-base font-semibold tracking-tight">Lumen Workspace</span>
          <span className="rounded-md bg-secondary px-2 py-0.5 text-xs text-secondary-foreground">
            demo
          </span>
        </div>
        <div className="flex items-center gap-4">
          <span className="text-sm text-muted-foreground">agent@example.com</span>
          <Button variant="outline" size="sm" onClick={handleLogout}>
            <LogOut className="mr-2" />
            退出登录
          </Button>
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">
        <aside className="flex w-56 shrink-0 flex-col border-r bg-muted/30">
          <nav className="flex-1 space-y-1 px-3 py-4">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-primary text-primary-foreground'
                      : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
                  )
                }
              >
                <item.icon />
                <span>{item.label}</span>
              </NavLink>
            ))}
          </nav>
          <div className="border-t px-4 py-3 text-xs text-muted-foreground">
            Stage 9.2 脚手架就绪
          </div>
        </aside>

        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
