export const INITIAL_VISIBLE_CHAT_MESSAGES = 60;
export const CHAT_MESSAGE_PAGE_SIZE = 60;

export interface ChatMessageWindow {
  startIndex: number;
  hiddenCount: number;
}

export function chatMessageWindow(total: number, visibleLimit: number): ChatMessageWindow {
  const normalizedTotal = Math.max(0, Math.floor(total));
  const normalizedLimit = Math.max(1, Math.floor(visibleLimit));
  const startIndex = Math.max(0, normalizedTotal - normalizedLimit);
  return {
    startIndex,
    hiddenCount: startIndex,
  };
}
