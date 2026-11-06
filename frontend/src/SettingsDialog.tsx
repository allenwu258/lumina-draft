import { useEffect, useRef, type ReactNode } from 'react';

type Props = {
  label: string;
  children: ReactNode;
  onClose: () => void;
  busy?: boolean;
  className?: string;
};
const focusableSelector =
  'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]';

export function SettingsDialog({ label, children, onClose, busy = false, className = '' }: Props) {
  const root = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  const busyRef = useRef(busy);
  closeRef.current = onClose;
  busyRef.current = busy;
  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    root.current?.querySelector<HTMLElement>(focusableSelector)?.focus();
    return () => {
      previouslyFocused?.focus();
    };
  }, []);
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busyRef.current) {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
      }
      if (event.key !== 'Tab') return;
      const elements = Array.from(
        root.current?.querySelectorAll<HTMLElement>(focusableSelector) || [],
      ).filter((element) => element.offsetParent !== null);
      const first = elements[0],
        last = elements[elements.length - 1];
      const outside = !root.current?.contains(document.activeElement);
      if (!first) {
        event.preventDefault();
        root.current?.focus();
      } else if (
        event.shiftKey &&
        (outside || document.activeElement === first || document.activeElement === root.current)
      ) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && (outside || document.activeElement === last)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, []);
  useEffect(() => {
    if (!busy && !root.current?.contains(document.activeElement)) {
      root.current?.querySelector<HTMLElement>(focusableSelector)?.focus();
    }
  }, [busy]);
  return (
    <div className="settings-modal-backdrop">
      <section
        ref={root}
        className={'settings-modal ' + className}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
      >
        {children}
      </section>
    </div>
  );
}
