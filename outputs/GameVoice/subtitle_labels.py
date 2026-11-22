"""Separate the coloured speaker label from the white dialogue before queuing."""
import re
from speaker_voices import split_speaker,split_fused_label,is_interface_label


def coloured_word(frame,box):
    left,top,right,bottom = [round(v) for v in box]
    # OCR can merge Name + the first dialogue word. Classify its leading
    # glyphs, rather than letting the following white word hide the label.
    crop = frame.crop((left,top,min(right,left+65),bottom)).convert('RGB')
    colour = white = 0
    for red,green,blue in crop.getdata():
        bright,dark = max(red,green,blue),min(red,green,blue)
        if bright >= 170 and bright-dark >= 65: colour += 1
        elif dark >= 160 and bright-dark <= 40: white += 1
    return colour >= 12 and colour > white*1.35


def separate_label(text,lines,known=()):
    # The complete line's delimiter outranks colour classification of a
    # single last glyph. OCR may mark the final letter of a name as white.
    explicit,body=split_speaker(text,known)
    if explicit and re.search(r'[:：]',text):
        return explicit,body
    if lines and lines[0].get('words'):
        words = lines[0]['words']
        start = None
        for index,word in enumerate(words[:4]):
            if word.get('coloured'):
                start=index;break
            # Allow tiny OCR specks before a name, never full dialogue words.
            if sum(char.isalpha() for char in word['text']) > 2: break
        if start is not None:
            end=start
            while end < min(len(words),start+4) and words[end].get('coloured'):
                end += 1
                if words[end-1]['text'].rstrip().endswith((':','：',';')): break
            label=' '.join(word['text'] for word in words[start:end]).strip(' :：;|—–-')
            tail=''
            delimiter=re.search(r'[:：;]',label)
            if delimiter:
                tail=label[delimiter.end():];label=label[:delimiter.start()]
            else:
                recovered,tail=split_speaker(label,known)
                if recovered:label=recovered
                else:label,tail=split_fused_label(label,known)
            body=' '.join([tail,*(word['text'] for word in words[end:]),
                           *(line['text'] for line in lines[1:])]).strip()
            # No speech is emitted for a frame containing only the label.
            if label and not is_interface_label(label): return label,body
    return split_speaker(text,known)


def caption_rows(lines):
    """Sort by baseline, then rejoin adjacent name/body OCR fragments left-to-right."""
    rows=[]
    for line in sorted(lines,key=lambda item:(item['box'][1],item['box'][0])):
        left,top,right,bottom=line['box']
        height=max(1,bottom-top)
        row=next((row for row in rows if any(
            min(bottom,other['box'][3])-max(top,other['box'][1]) >
            .5*min(height,other['box'][3]-other['box'][1]) for other in row)),None)
        if row is None:rows.append([line])
        else:row.append(line)
    result=[]
    for row in rows:
        joined=[]
        for line in sorted(row,key=lambda item:item['box'][0]):
            line={**line,'words':list(line.get('words',[])),'box':list(line['box'])}
            previous=joined[-1] if joined else None
            gap=line['box'][0]-previous['box'][2] if previous else 9999
            height=line['box'][3]-line['box'][1]
            if previous and -2<=gap<=max(12,height*.65):
                previous['text']+=' '+line['text']
                previous['words'].extend(line['words'])
                a,b=previous['box'],line['box']
                previous['box']=[min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3])]
            else:joined.append(line)
        result.extend(joined)
    return result


def separate_captions(text,lines,known=(),label_policy=None):
    """A new label begins a new speaker; white continuation rows stay together."""
    def identify(text,rows):
        name,body=separate_label(text,rows,known)
        if name and label_policy and not label_policy(name,rows,known):
            return '',text
        return name,body
    if not lines: return [identify(text,[])] if text.strip() else []
    lines=caption_rows(lines)
    groups=[]
    # A license plate beside the subtitle was recognized as a second OCR
    # line on the same baseline, then appended and spoken as a new suffix.
    # A wrapped continuation has its own row below, and remains eligible.
    labelled=[line for line in lines if identify(line['text'],[line])[0]
              and any(word.get('coloured') for word in line.get('words',[]))]
    previous_box=None
    for line in lines:
        label,body=identify(line['text'],[line])
        if not label:
            left,top,right,bottom=line['box']
            beside=False
            for caption in labelled:
                x1,y1,x2,y2=caption['box']
                overlap=min(bottom,y2)-max(top,y1)
                if overlap>.5*min(bottom-top,y2-y1) and (right<x1-4 or left>x2+4):
                    beside=True;break
            if beside:continue
        if not label and groups and previous_box is not None:
            left,top,right,bottom=line['box'];a,b,c,d=previous_box
            height=max(bottom-top,d-b)
            # Wrapped subtitles stay near the same centre and next baseline;
            # a vehicle card/menu column on the right is a separate block.
            continuation=(top>=d-2 and top-d<=height*1.2 and
                          abs((left+right-a-c)/2)<=max(150,(c-a)*.3))
            if continuation:
                groups[-1][1]=' '.join((groups[-1][1],body)).strip()
                previous_box=line['box']
                continue
        if label or not groups:
            groups.append([label,body]);previous_box=line['box']
        # An isolated unlabelled block never attaches to a named caption.
    return groups
