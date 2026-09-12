import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';

import { JWT_STORAGE_KEY } from '@/lib/api-client';

export interface AuthGuardProps {
  children: ReactNode;
}

export function AuthGuard({ children }: AuthGuardProps): JSX.Element {
  const token = window.localStorage.getItem(JWT_STORAGE_KEY);
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}
