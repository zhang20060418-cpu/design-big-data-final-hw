"""Print the existing self-contained report with an isolated local browser."""
from pathlib import Path
import os,subprocess

def export_pdf(task_dir: Path) -> Path:
    folder=Path(task_dir)/'report';html=folder/'report.html';target=folder/'report.pdf'
    if not html.exists():raise ValueError('HTML报告尚未生成。')
    if target.exists() and target.stat().st_mtime >= html.stat().st_mtime:return target
    candidates=[Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe',
                Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'Microsoft/Edge/Application/msedge.exe',
                Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'Google/Chrome/Application/chrome.exe']
    browser=next((p for p in candidates if p.is_file()),None)
    if browser is None:raise RuntimeError('未找到本机Edge/Chrome打印引擎，可先导出HTML后用浏览器打印。')
    profile=folder/'pdf_browser_profile';profile.mkdir(exist_ok=True)
    command=[str(browser),'--headless=new','--disable-gpu','--no-first-run','--no-pdf-header-footer',
             '--user-data-dir='+str(profile.resolve()),'--print-to-pdf='+str(target.resolve()),html.resolve().as_uri()]
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=0x08000000 if os.name=='nt' else 0)
    try:stdout,stderr=proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        if os.name=='nt':subprocess.run(['taskkill','/F','/T','/PID',str(proc.pid)],capture_output=True,creationflags=0x08000000)
        else:proc.kill()
        raise RuntimeError('PDF打印超时，HTML报告已保留。')
    if not target.is_file() or target.stat().st_size<100 or target.read_bytes()[:5]!=b'%PDF-':raise RuntimeError('浏览器未生成有效PDF，HTML报告已保留。')
    return target
