"""Numeric-only cached MQTT samples and a text-free Pillow plot."""
import math
import re

from PIL import Image, ImageDraw

_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
BACKGROUND = '#1c2b3c'
GRID = '#30465a'
TRACE = '#46ddb0'


def _empty(status):
    return {'status':status, 'points':[], 'latest':None, 'min':None, 'max':None}


def numeric_series(messages):
    """Accept only complete numeric bodies with finite nonnegative Unix times.

    One invalid cached message rejects the whole chart; no invalid message is
    silently skipped and no zero, interpolated or synthetic sample is added.
    Equal timestamps retain their original input order.
    """
    if messages is None:
        return _empty('empty')
    points=[]
    try:
        iterator=iter(messages)
    except TypeError:
        return _empty('non_numeric')
    for message in iterator:
        if not isinstance(message,dict) or message.get('truncated',False):
            return _empty('non_numeric')
        body=message.get('text')
        stamp=message.get('timestamp')
        if not isinstance(body,str) or not _NUMBER.fullmatch(body.strip()):
            return _empty('non_numeric')
        if type(stamp) not in (int,float):
            return _empty('non_numeric')
        try:
            value=float(body.strip())
            timestamp=float(stamp)
        except (ValueError,OverflowError):
            return _empty('non_numeric')
        if not math.isfinite(value) or not math.isfinite(timestamp) or timestamp<0:
            return _empty('non_numeric')
        points.append((timestamp,value))
    if not points:
        return _empty('empty')
    points.sort(key=lambda point:point[0])
    values=[value for _,value in points]
    return {'status':'numeric','points':points,'latest':points[-1][1],
            'min':min(values),'max':max(values)}


def _fraction(value, lower, upper):
    if lower==upper:
        return .5
    span=upper-lower
    if math.isfinite(span):
        ratio=(value-lower)/span
    else:
        # Opposite large finite endpoints can have an infinite difference.
        # Scaling first bounds both endpoints to [-1,1].
        scale=max(abs(lower),abs(upper))
        scaled_lower=lower/scale
        ratio=(value/scale-scaled_lower)/(upper/scale-scaled_lower)
    return max(0.,min(1.,ratio))


def render_plot(series,width=408,height=170):
    """Render only grid, line and real points; callers own labels and units."""
    if type(width) is not int or type(height) is not int or width<1 or height<1:
        raise ValueError('Plot dimensions must be positive integers')
    plot=Image.new('RGB',(width,height),BACKGROUND)
    draw=ImageDraw.Draw(plot)
    pad_x=min(12,(width-1)//4)
    pad_y=min(12,(height-1)//4)
    left,right=pad_x,width-1-pad_x
    top,bottom=pad_y,height-1-pad_y
    for step in range(5):
        x=round(left+(right-left)*step/4)
        y=round(top+(bottom-top)*step/4)
        draw.line((x,top,x,bottom),fill=GRID)
        draw.line((left,y,right,y),fill=GRID)
    if not isinstance(series,dict) or series.get('status')!='numeric':
        return plot
    points=series.get('points')
    if not isinstance(points,(list,tuple)) or not points:
        return plot
    # Keep the renderer safe if a caller passes a malformed series directly.
    try:
        if any(len(point)!=2 or any(type(v) not in (int,float) or not math.isfinite(v)
                                    for v in point) for point in points):
            return plot
    except (TypeError,ValueError,OverflowError):
        return plot
    times=[point[0] for point in points]
    values=[point[1] for point in points]
    t0,t1=min(times),max(times)
    low,high=min(values),max(values)
    pixels=[(round(left+_fraction(t,t0,t1)*(right-left)),
             round(bottom-_fraction(value,low,high)*(bottom-top))) for t,value in points]
    if len(pixels)>1:
        draw.line(pixels,fill=TRACE,width=2)
    radius=min(3,max(1,min(width,height)//12))
    for x,y in pixels:
        draw.ellipse((x-radius,y-radius,x+radius,y+radius),fill=TRACE)
    return plot
