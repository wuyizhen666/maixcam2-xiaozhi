import importlib.util
import math
from pathlib import Path
import sys
import unittest

PATH=Path(__file__).resolve().parents[1]/'apps/siot_monitor/numeric_chart.py'
spec=importlib.util.spec_from_file_location('siot_numeric_chart',PATH)
chart=importlib.util.module_from_spec(spec)
spec.loader.exec_module(chart)


def message(text,timestamp=1,**kwargs):
    return dict(text=text,timestamp=timestamp,**kwargs)


class NumericSeriesTests(unittest.TestCase):
    def test_empty_cache_has_no_invented_sample(self):
        series=chart.numeric_series([])
        self.assertEqual(series['status'],'empty')
        self.assertEqual(series['points'],[])
        self.assertIsNone(series['latest'])

    def test_signed_decimal_and_scientific_values_are_sorted_stably(self):
        series=chart.numeric_series([message('  -2.5  ',3),message('+1.25e2',1),
                                    message('.5',2),message('3.',2)])
        self.assertEqual(series['status'],'numeric')
        self.assertEqual(series['points'],[(1.,125.),(2.,.5),(2.,3.),(3.,-2.5)])
        self.assertEqual(series['latest'],-2.5)
        self.assertEqual(series['min'],-2.5)
        self.assertEqual(series['max'],125.)

    def test_any_invalid_payload_makes_whole_cache_non_numeric(self):
        for text in ['NaN','inf','-Infinity','1e309','true','False','{"v":1}',
                     '[1,2]','25 C','1 2','','  ',True,3,None,'1,000']:
            with self.subTest(text=text):
                series=chart.numeric_series([message('1',1),message(text,2)])
                self.assertEqual(series['status'],'non_numeric')
                self.assertEqual(series['points'],[])
                self.assertIsNone(series['min'])

    def test_truncated_or_invalid_timestamp_rejects_entire_series(self):
        bad=[message('1',1,truncated=True)]
        bad += [message('1',t) for t in [None,'10',True,float('nan'),float('inf'),-1]]
        bad.append({'text':'1'})
        for value in bad:
            with self.subTest(value=value):
                self.assertEqual(chart.numeric_series([message('2',0),value])['status'],'non_numeric')


class PlotTests(unittest.TestCase):
    GREEN=(70,221,176)

    def test_non_numeric_and_empty_series_have_no_green_data(self):
        for series in [chart.numeric_series([]),chart.numeric_series([message('text')])]:
            plot=chart.render_plot(series)
            self.assertEqual(plot.mode,'RGB')
            self.assertEqual(plot.size,(408,170))
            self.assertNotIn(self.GREEN,list(plot.getdata()))

    def test_single_point_is_centered(self):
        plot=chart.render_plot(chart.numeric_series([message('20',10)]))
        self.assertEqual(plot.getpixel((204,84)),self.GREEN)

    def test_constant_values_are_horizontal_without_division_by_zero(self):
        plot=chart.render_plot(chart.numeric_series([message('2',0),message('2',10)]))
        self.assertEqual(plot.getpixel((204,84)),self.GREEN)
        self.assertNotEqual(plot.getpixel((204,20)),self.GREEN)

    def test_actual_time_intervals_determine_x_positions(self):
        plot=chart.render_plot(chart.numeric_series([message('0',0),message('1',1),message('0',10)]))
        self.assertEqual(plot.getpixel((50,12)),self.GREEN)
        self.assertNotEqual(plot.getpixel((204,12)),self.GREEN)

    def test_extreme_finite_values_do_not_overflow_the_span(self):
        limit=sys.float_info.max
        series=chart.numeric_series([message(str(-limit),0),message('0',1),message(str(limit),2)])
        self.assertEqual(series['status'],'numeric')
        plot=chart.render_plot(series)
        self.assertEqual(plot.getpixel((12,157)),self.GREEN)
        self.assertEqual(plot.getpixel((395,12)),self.GREEN)
        self.assertEqual(plot.getpixel((204,84)),self.GREEN)

    def test_subnormal_and_equal_timestamps_render_without_fake_spacing(self):
        series=chart.numeric_series([message('-5e-324',2),message('5e-324',2)])
        plot=chart.render_plot(series)
        self.assertEqual(plot.getpixel((204,12)),self.GREEN)
        self.assertEqual(plot.getpixel((204,157)),self.GREEN)

    def test_invalid_sizes_are_rejected(self):
        for width,height in [(0,170),(408,-1),(True,10),(1.5,170)]:
            with self.assertRaises(ValueError):chart.render_plot(chart.numeric_series([]),width,height)


if __name__=='__main__':unittest.main()
