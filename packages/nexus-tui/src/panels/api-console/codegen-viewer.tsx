/**
 * Code generation panel showing request as curl/fetch/python snippets.
 */

import React, { useState } from "react";
import { useApiConsoleStore } from "../../stores/api-console-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { generateCode, type CodegenLanguage } from "./codegen.js";

const LANGUAGES: readonly CodegenLanguage[] = ["curl", "fetch", "python"];

export function CodegenViewer(): React.ReactNode {
  const [lang, setLang] = useState<CodegenLanguage>("curl");
  const request = useApiConsoleStore((s) => s.request);
  const baseUrl = useGlobalStore((s) => s.config.baseUrl ?? "http://localhost:2026");

  const code = generateCode(lang, request, baseUrl);

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Language selector */}
      <box height={1} width="100%" flexDirection="row">
        {LANGUAGES.map((l) => {
          const active = l === lang ? "▸ " : "  ";
          return <text key={l}>{`${active}${l}`}</text>;
        })}
      </box>

      {/* Code output */}
      <scrollbox flexGrow={1} width="100%">
        <code content={code} filetype={lang === "curl" ? "bash" : lang === "fetch" ? "javascript" : "python"} syntaxStyle={undefined!} />
      </scrollbox>
    </box>
  );
}
