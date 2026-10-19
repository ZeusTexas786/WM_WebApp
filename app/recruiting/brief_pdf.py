"""Single-page, bounded visual brief; the evidence view retains unabridged text."""
import io
from pathlib import Path
from xml.sax.saxutils import escape
import reportlab
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_ROOT=Path(reportlab.__file__).parent/'fonts'
pdfmetrics.registerFont(TTFont('BriefSans',str(FONT_ROOT/'Vera.ttf')))
pdfmetrics.registerFont(TTFont('BriefBold',str(FONT_ROOT/'VeraBd.ttf')))
INK=HexColor('#203c30'); MUTED=HexColor('#5e7064'); GREEN=HexColor('#2b6046')

def clip(value,limit):
    value=str(value).replace('\u2014',' - ').replace('\u2013','-')
    return escape(value if len(value)<=limit else value[:limit-3].rstrip()+'...')

def paragraph(value,size=9,color=INK,bold=False):
    return Paragraph(value,ParagraphStyle('brief',fontName='BriefBold' if bold else 'BriefSans',fontSize=size,leading=size*1.5,textColor=color,spaceAfter=0))

def draw_paragraph(c,value,x,y,width,size=9,color=INK,bold=False):
    p=paragraph(value,size,color,bold); _,height=p.wrap(width,1000); p.drawOn(c,x,y-height)
    return y-height

def column(c,items,x,top,width,available):
    # Scale measured content as a group, never clip overflowing text.
    for scale in (1,.95,.9,.85):
        measured=[(paragraph(value,size*scale,color,bold),gap*scale) for value,size,color,bold,gap in items]
        heights=[p.wrap(width,1000)[1]+gap for p,gap in measured]
        if sum(heights)<=available: break
    else: raise ValueError('Brief is too dense for a readable single-page PDF. Use the full evidence view.')
    y=top
    for (p,gap),height in zip(measured,heights):
        actual=height-gap; p.drawOn(c,x,y-actual); y-=height

def create_brief_pdf(candidate,job):
    result=job.result
    output=io.BytesIO(); c=canvas.Canvas(output,pagesize=A4,pageCompression=1)
    width,height=A4
    c.setTitle('WebMonkie candidate brief'); c.setAuthor('WebMonkie')
    c.setFillColor(HexColor('#f7f8f3')); c.rect(0,0,width,height,fill=1,stroke=0)
    c.setFillColor(HexColor('#e7efdd')); c.rect(0,height-195,width,195,fill=1,stroke=0)
    draw_paragraph(c,'webmonkie.',40,height-30,300,17,GREEN,True)
    label='CANDIDATE BRIEF / SYNTHETIC SAMPLE' if result.get('sample') else 'CANDIDATE BRIEF / RESUME EVIDENCE'
    draw_paragraph(c,label,40,height-65,400,8,GREEN,True)
    draw_paragraph(c,clip(result['name'],50),40,height-89,410,25,INK,True)
    draw_paragraph(c,clip(result['headline'],100),40,height-132,400,9,MUTED)
    date=job.completed_at.strftime('%b %d, %Y') if job.completed_at else 'Local preview'
    draw_paragraph(c,clip(candidate.session.title,70)+f' / Rubric v{candidate.rubric_version} / {date}',40,height-158,460,8,MUTED)
    c.setFillColor(HexColor('#d0e2ba')); c.circle(width-70,height-107,32,fill=1,stroke=0)
    c.setFillColor(GREEN); c.setFont('BriefBold',22); c.drawCentredString(width-70,height-105,str(round(result['alignment'])))
    c.setFont('BriefSans',6.5); c.drawCentredString(width-70,height-119,'ALIGNMENT')
    draw_paragraph(c,clip(result['summary'],240),40,height-217,width-80,10,MUTED)
    left=[]; right=[]
    def heading(items,label): items.append((label,8,GREEN,True,10))
    def item(items,title,body,limit=145):
        items.append((clip(title,65),9,INK,True,3))
        items.append((clip(body,limit),8.5,MUTED,False,12))
    heading(left,'WHY THIS CANDIDATE STANDS OUT')
    strengths=[e for e in result['evaluations'] if e['status']=='met'][:3]
    for entry in strengths: item(left,entry['name'],entry['explanation'])
    if not strengths: left.append(('No fully demonstrated requirements yet. Explore partial evidence and ask for concrete examples.',9,MUTED,False,15))
    heading(left,'EVIDENCE AGAINST YOUR PRIORITIES')
    for entry in result['evaluations'][:4]:
        body=(entry['evidence'][0]['quote']+' ['+entry['evidence'][0]['location']+']') if entry['evidence'] else 'Not established in the supplied resume.'
        item(left,entry['name']+' / '+entry['status'].upper(),body,140)
    heading(right,'CLARIFY IN CONVERSATION')
    gaps=[e for e in result['evaluations'] if e['status']!='met'][:3]
    for entry in gaps: item(right,entry['name'],entry['explanation'])
    if not gaps: right.append(('Ask for a concrete project walkthrough to verify ownership, decisions, and outcomes.',9,MUTED,False,20))
    heading(right,'CONSISTENCY &amp; VERIFICATION')
    for concern in result['concerns'][:2]: item(right,concern['issue'],'Ask: '+concern['question'],150)
    if not result['concerns']: right.append(('No specific contradiction flagged. This is not proof that all claims are accurate.',9,MUTED,False,20))
    heading(right,'HOW TO READ THIS BRIEF')
    right.append((f"<b>{result['coverage']:.0f}% evidence coverage.</b> Unknown differs from mismatch. Alignment measures your weighted rubric; it does not predict job success.",9,MUTED,False,15))
    top=height-295; available=top-105
    column(c,left,40,top,290,available)
    column(c,right,355,top,width-395,available)
    c.setStrokeColor(HexColor('#cbd8c2')); c.line(40,86,width-40,86)
    footer=f'Condensed brief: up to four criteria and selected findings. Full evidence and all {len(result["evaluations"])} criteria remain in the workspace. Quotations are resume claims, not independent verification. Hiring decisions remain yours.'
    draw_paragraph(c,footer,40,74,width-80,7,MUTED)
    c.showPage(); c.save()
    return output.getvalue()
