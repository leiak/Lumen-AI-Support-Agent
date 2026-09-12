import { Navigate, Route, Routes } from 'react-router-dom';

import { AppShell } from '@/components/layout/app-shell';
import { AuthGuard } from '@/components/auth/auth-guard';
import { LoginPage } from '@/pages/login';
import { InboxPage } from '@/pages/inbox';
import { InboxDetailPage } from '@/pages/inbox-detail';
import { KbPage } from '@/pages/kb';
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
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="/" element={<Navigate to="/inbox" replace />} />
      <Route path="*" element={<Navigate to="/inbox" replace />} />
    </Routes>
  );
}
