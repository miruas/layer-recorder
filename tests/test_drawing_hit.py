import math
import unittest
from katman.editing import stroke_hit

class EraserGeometry(unittest.TestCase):
    def test_line_and_sparse_pen_midpoints(self):
        for tool in ('line','arrow','pen'):
            stroke={'tool':tool,'width':.004,'points':[[.1,.4],[.9,.4]]}
            self.assertTrue(stroke_hit(stroke,[.5,.4],(1000,500)))
            self.assertFalse(stroke_hit(stroke,[.5,.8],(1000,500)))

    def test_arrowhead_segments_and_different_canvas_scales(self):
        stroke={'tool':'arrow','width':.02,'points':[[.1,.4],[.9,.4]]}
        for w,h in ((1000,500),(300,150)):
            length=max(10,stroke['width']*h*4)
            point=[(.9*w-length*.8*math.cos(.5))/w,(.4*h-length*.8*math.sin(.5))/h]
            self.assertTrue(stroke_hit(stroke,point,(w,h)))

    def test_filled_privacy_region_and_missed_region(self):
        stroke={'tool':'redact','points':[[.2,.2],[.4,.4]]}
        self.assertTrue(stroke_hit(stroke,[.3,.3],(1000,500)))
        self.assertFalse(stroke_hit(stroke,[.6,.6],(1000,500)))
