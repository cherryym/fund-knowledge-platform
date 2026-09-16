// Presentation only: the API remains responsible for sanitizing document HTML.
// Keep the original document, evidence anchors and any backend CSP intact.
const previewStyles = `
html { color-scheme: light; background: #fff; }
*, *::before, *::after { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 32px; width: 100%; max-width: 960px;
  font-family: "PingFang SC", "Noto Sans SC", "Microsoft YaHei", system-ui, sans-serif;
  font-size: 14px; line-height: 1.9; font-weight: 400;
  color: #34445c; background: #fff; overflow-wrap: anywhere;
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
}
h1, h2, h3, h4, h5, h6 { color: #213653; font-weight: 600; line-height: 1.55; overflow-wrap: anywhere; }
h1 { font-size: 22px; margin: 0 0 22px; }
h2 { font-size: 19px; margin: 28px 0 14px; }
h3 { font-size: 17px; margin: 24px 0 12px; }
h4, h5, h6 { font-size: 15px; margin: 20px 0 10px; }
p { margin: 0 0 14px; white-space: pre-wrap; }
ul, ol { margin: 0 0 18px; padding-left: 24px; }
li { margin: 5px 0; }
table { border-collapse: collapse; table-layout: fixed; width: 100%; max-width: 100%; margin: 18px 0 24px; font-size: 13px; line-height: 1.8; }
th, td { border: 1px solid #e3e9f1; padding: 10px 12px; text-align: left; vertical-align: top; overflow-wrap: anywhere; word-break: normal; white-space: pre-wrap; }
th { background: #f6f8fc; color: #465b78; font-weight: 550; }
caption { text-align: left; color: #6b7e99; font-size: 12px; padding-bottom: 9px; }
img { display: block; max-width: 100%; height: auto; margin: 18px auto; }
figure { margin: 20px 0; } figcaption { font-size: 12px; color: #7f8da2; }
pre, code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; background: #f7f9fc; padding: 14px 16px; border-radius: 5px; }
blockquote { margin: 18px 0; padding: 10px 18px; border-left: 3px solid #cddcf3; background: #f8faff; color: #657a98; }
a { color: #315fd4; text-underline-offset: 3px; }
hr { border: 0; border-top: 1px solid #e5eaf2; margin: 26px 0; }
.page { max-width: 100%; margin: 24px auto; padding: 20px; border: 1px solid #e6ebf2; }
.notice { margin: 0 0 20px; padding: 12px 16px; border-radius: 5px; font-size: 12px; color: #8b754b; background: #fffaf0; }
small { font-size: 12px; color: #8492a7; }
@media (max-width: 600px) { body { padding: 28px; } th, td { padding: 8px; } }
`;
const styleElement = '<style id="fund-kb-preview-base">' + previewStyles + '</style>';
const fragmentHead = '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
  + '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
  + styleElement;

export function stylePreviewDocument(sanitizedHtml: string): string {
  if (/<style\b[^>]*\bid=["']fund-kb-preview-base["']/i.test(sanitizedHtml)) return sanitizedHtml;
  // Append after the backend's styles so typography can override browser defaults.
  if (/<\/head\s*>/i.test(sanitizedHtml)) return sanitizedHtml.replace(/<\/head\s*>/i, styleElement + '$&');
  if (/<body\b[^>]*>/i.test(sanitizedHtml)) return sanitizedHtml.replace(/<body\b[^>]*>/i, '<head>' + fragmentHead + '</head>$&');
  return '<!doctype html><html lang="zh-CN"><head>' + fragmentHead + '</head><body>' + sanitizedHtml + '</body></html>';
}
