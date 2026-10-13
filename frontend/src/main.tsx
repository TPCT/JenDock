import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "@primer/primitives/dist/css/primitives.css";
import "@primer/primitives/dist/css/functional/themes/light.css";
import "@primer/primitives/dist/css/functional/themes/dark.css";
import "@primer/primitives/dist/css/functional/themes/dark-dimmed.css";
import "./styles/global.css";

async function start(): Promise<void> {
  if (import.meta.env.MODE === "mock") {
    try {
      const { worker } = await import("./mocks/browser");
      await worker.start({ onUnhandledFrame: "bypass" });
    } catch (error) {
      const root = document.getElementById("root");
      if (root) root.textContent = `Mock mode could not start its service worker: ${String(error)}`;
      throw error;
    }
  }
  const root = document.getElementById("root");
  if (!root) throw new Error("Missing #root element");
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void start();
