import { Navigate, Route, Routes } from 'react-router-dom';

import { AppShell } from '@/components/layout/app-shell';
import { AdminGuard } from '@/components/auth/admin-guard';
import { AuthGuard } from '@/components/auth/auth-guard';
import { AdminKbDraftsPage } from '@/pages/admin-kb-drafts';
import { AdminSlaPoliciesPage } from '@/pages/admin-sla-policies';
import { AdminBudgetPage } from '@/pages/admin-budget';
import { AdminTicketsPage } from '@/pages/admin-tickets';
import { AdminTicketDetailPage } from '@/pages/admin-ticket-detail';
import { LoginPage } from '@/pages/login';
import { InboxPage } from '@/pages/inbox';
import { InboxDetailPage } from '@/pages/inbox-detail';
import { KbPage } from '@/pages/kb';
import { KbDetailPage } from '@/pages/kb-detail';
import { ArticleDetailPage } from '@/pages/article-detail';
import { SettingsPage } from '@/pages/settings';

export function App(): JSX.Element {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <AuthGuard>
            <AppShell />
          </AuthGuard>
        }
      >
        <Route path="/inbox" element={<InboxPage />} />
        <Route path="/inbox/:id" element={<InboxDetailPage />} />
        <Route path="/kb" element={<KbPage />} />
        <Route path="/kb/:kbId" element={<KbDetailPage />} />
        <Route path="/kb/:kbId/articles/:articleId" element={<ArticleDetailPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route
          path="/admin/sla-policies"
          element={
            <AdminGuard>
              <AdminSlaPoliciesPage />
            </AdminGuard>
          }
        />
        <Route
          path="/admin/budget"
          element={
            <AdminGuard>
              <AdminBudgetPage />
            </AdminGuard>
          }
        />
        <Route
          path="/admin/tickets"
          element={
            <AdminGuard>
              <AdminTicketsPage />
            </AdminGuard>
          }
        />
        <Route
          path="/admin/tickets/:id"
          element={
            <AdminGuard>
              <AdminTicketDetailPage />
            </AdminGuard>
          }
        />
        <Route
          path="/admin/kb-drafts"
          element={
            <AdminGuard>
              <AdminKbDraftsPage />
            </AdminGuard>
          }
        />
      </Route>
      <Route path="/" element={<Navigate to="/inbox" replace />} />
      <Route path="*" element={<Navigate to="/inbox" replace />} />
    </Routes>
  );
}
