import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.jsx";
import "./styles.css";
import "./readability.css";
import "./document-canvas.css";
import "./motion.css";
import "./wiki-integration.css";
import "./ambient-effects.css";
import "./app-shell.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
