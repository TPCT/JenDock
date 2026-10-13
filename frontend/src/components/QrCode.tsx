import qrcode from "qrcode-generator";
import { useMemo } from "react";
import styles from "./QrCode.module.css";

const QUIET_ZONE = 4;

/**
 * A QR code drawn as inline SVG from `value`, entirely in the browser (no image service, no
 * data: URL, nothing for the CSP to block). Dark modules use `currentColor`, the background is
 * always white so scanners work in dark mode too.
 */
export function QrCode({ value, label }: { value: string; label: string }) {
  const { size, path } = useMemo(() => {
    const code = qrcode(0, "M");
    code.addData(value);
    code.make();
    const count = code.getModuleCount();
    let d = "";
    for (let row = 0; row < count; row += 1) {
      let col = 0;
      while (col < count) {
        if (!code.isDark(row, col)) {
          col += 1;
          continue;
        }
        const start = col;
        while (col < count && code.isDark(row, col)) col += 1;
        d += `M${String(start + QUIET_ZONE)} ${String(row + QUIET_ZONE)}h${String(col - start)}v1h-${String(col - start)}z`;
      }
    }
    return { size: count + QUIET_ZONE * 2, path: d };
  }, [value]);
  return (
    <svg
      className={styles.qr}
      viewBox={`0 0 ${String(size)} ${String(size)}`}
      role="img"
      aria-label={label}
      shapeRendering="crispEdges"
    >
      <rect width={size} height={size} fill="#ffffff" />
      <path d={path} fill="#000000" />
    </svg>
  );
}
