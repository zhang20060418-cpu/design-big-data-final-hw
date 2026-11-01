"""14 native slides for functional integration QA; no prediction/label fixture."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from pptx import Presentation
from pptx.dml.color import RGBColor
from app.services.demo import _background,_text,_rect,INK,MUTED

def build():
 source=ROOT.parent/'fixtures/demo_deck.pptx';target=ROOT.parent/'fixtures/integration_14_slides.pptx'
 if target.exists():return target
 deck=Presentation(str(source));blank=deck.slide_layouts[6]
 slide=deck.slides.add_slide(blank);_background(slide)
 _text(slide,.8,.55,11,.65,'空间利用复核',32,INK,True)
 _text(slide,.85,2.4,2.25,1.65,'阶段复盘需要核对用户需求、页面用途与主要信息。此处保留较多空白，供审查者判断这是刻意强调，还是内容尺度和位置不协调。',12,MUTED)
 for number,subject in enumerate(['信息采集','交付检查','成果回访'],1):
  slide=deck.slides.add_slide(blank);_background(slide)
  _text(slide,.8,.55,11,.65,f'{subject}：执行模块',32,INK,True)
  for column,(title,body) in enumerate([
   ('执行目标',f'{subject}需要明确目标、责任与验收条件。团队保留原始资料，核查关键内容是否完整，并将无法确认的事项留给后续复核。'),
   ('证据记录',f'{subject}阶段保留文稿与页面定位。每条结论都应对应实际对象或可见区域，不应把缺少信息自动解释为已经合格。'),
   ('后续安排',f'{subject}完成后核对记录与结论。需要调整的页面先明确问题位置，再讨论可执行的建议，并保留修改前后的审查历史。')]):
   x=.8+4.13*column
   _rect(slide,x,2.05,3.75,3.75,RGBColor(255,255,255))
   _text(slide,x+.22,2.3,3.25,.5,title,23,INK,True)
   _text(slide,x+.22,3.1,3.25,2.25,body,17,MUTED)
 assert len(deck.slides)==14
 deck.save(str(target));return target
if __name__=='__main__':print(build())
