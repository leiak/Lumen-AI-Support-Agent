import { useParams } from 'react-router-dom';

export function InboxDetailPage(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  return (
    <div className="p-6">
      会话详情 — Stage 9.5 实现(conversationId: <code>{id}</code>)
    </div>
  );
}
