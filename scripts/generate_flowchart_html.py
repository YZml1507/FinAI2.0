#!/usr/bin/env python3
"""生成项目状态流程图 HTML 版本"""
import re

def generate_flowchart_html():
    md = open("docs/project_status_flowchart.md", "r", encoding="utf-8").read()

    html_template = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>FinAI2.0 项目状态流程图</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 1400px;
    margin: 40px auto;
    padding: 0 20px;
    background: #0f172a;
    color: #f8fafc;
}}
h1, h2 {{ color: #38bdf8; }}
.mermaid {{
    background: white;
    padding: 20px;
    border-radius: 8px;
    margin: 20px 0;
}}
pre {{
    background: #1e293b;
    padding: 15px;
    border-radius: 8px;
    overflow-x: auto;
}}
</style>
</head>
<body>
{content}
<script>mermaid.initialize({{ startOnLoad: true, theme: "default" }});</script>
</body>
</html>"""

    # Convert markdown to HTML
    content = md.replace("# ", "<h1>").replace("</h1>", "")
    content = content.replace("## ", "<h2>")
    content = re.sub(r"```mermaid\n(.*?)\n```", r'<div class="mermaid">\n\1\n</div>', content, flags=re.DOTALL)
    content = content.replace("\n\n", "<br/><br/>")

    html = html_template.format(content=content)

    with open("docs/project_status_flowchart.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("✅ HTML flowchart generated: docs/project_status_flowchart.html")

if __name__ == "__main__":
    generate_flowchart_html()
