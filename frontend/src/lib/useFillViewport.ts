import { useCallback, useEffect, useRef, useState } from "react";

/** Fill the viewport below this element's actual position in the surrounding page layout. */
export function useFillViewport(): (element: HTMLElement | null) => void {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const elementRef = useRef<HTMLElement | null>(null);
  const ref = useCallback((nextElement: HTMLElement | null) => {
    elementRef.current = nextElement;
    setElement(nextElement);
  }, []);

  useEffect(() => {
    const target = elementRef.current;
    if (!target) return;

    const update = () => {
      if (window.matchMedia("(max-width: 767px)").matches) {
        target.style.height = "";
        return;
      }
      const padding = window.matchMedia("(min-width: 1012px)").matches ? 24 : 16;
      const top = target.getBoundingClientRect().top + window.scrollY;
      target.style.height = `${Math.max(448, window.innerHeight - top - padding)}px`;
    };

    update();
    window.addEventListener("resize", update);
    const observer = new ResizeObserver(update);
    observer.observe(document.body);
    return () => {
      window.removeEventListener("resize", update);
      observer.disconnect();
      target.style.height = "";
    };
  }, [element]);

  return ref;
}
