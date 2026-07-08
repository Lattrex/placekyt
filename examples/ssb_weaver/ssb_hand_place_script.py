# placeKYT command trace (runnable replay) — replay with:
#   placekyt --replay this_file.py
# or inside the placeKYT console where `controller` exists:
#   exec(open('this_file.py').read())
ctrl = controller  # the live AppController

ctrl.import_grc(path='/home/system/placekyt/examples/ssb_weaver/ssb_weaver.grc', name='ssb_weaver.grc', chip_type='kyttar_10x12')  # Import GNURadio flowgraph ssb_weaver.grc
ctrl.auto_place(chip=0)  # Auto-place blocks
ctrl.transform_block(block_name='gain', kind='cw')  # Rotate CW gain
ctrl.transform_block(block_name='gain', kind='cw')  # Rotate CW gain
ctrl.transform_block(block_name='complexmixer', kind='ccw')  # Rotate CCW complexmixer
ctrl.transform_block(block_name='complexmixer', kind='mirror_v')  # Mirror V complexmixer
ctrl.transform_block(block_name='complexmixer', kind='cw')  # Rotate CW complexmixer
ctrl.transform_block(block_name='complexmixer', kind='cw')  # Rotate CW complexmixer
ctrl.move_block(block_name='complexmixer', dx=-2, dy=5)  # Move complexmixer
ctrl.transform_block(block_name='complexmixer', kind='mirror_v')  # Mirror V complexmixer
ctrl.move_block(block_name='complexlowpassfilter', dx=2, dy=2)  # Move complexlowpassfilter
ctrl.transform_block(block_name='complexlowpassfilter', kind='ccw')  # Rotate CCW complexlowpassfilter
ctrl.move_block(block_name='iqupconvert', dx=-1, dy=1)  # Move iqupconvert
ctrl.move_block(block_name='complexlowpassfilter_2', dx=0, dy=1)  # Move complexlowpassfilter_2
ctrl.transform_block(block_name='complexmixer_2', kind='ccw')  # Rotate CCW complexmixer_2
ctrl.move_block(block_name='complexmixer_2', dx=0, dy=-1)  # Move complexmixer_2
ctrl.transform_block(block_name='complexmixer_2', kind='mirror_v')  # Mirror V complexmixer_2
ctrl.move_block(block_name='iqupconvert_2', dx=0, dy=1)  # Move iqupconvert_2
ctrl.set_route_group(names=['net2', 'net11'], points=[[6, 8], [6, 7], [7, 7]])  # Route connection (I/Q pair)
ctrl.set_route_group(names=['net1', 'net10'], points=[[4, 11], [5, 11], [6, 11]])  # Route connection (I/Q pair)
ctrl.transform_block(block_name='iqupconvert_2', kind='cw')  # Rotate CW iqupconvert_2
ctrl.move_block(block_name='iqupconvert_2', dx=0, dy=-2)  # Move iqupconvert_2
ctrl.transform_block(block_name='iqupconvert_2', kind='mirror_h')  # Mirror H iqupconvert_2
ctrl.move_block(block_name='complexlowpassfilter_2', dx=0, dy=3)  # Move complexlowpassfilter_2
ctrl.transform_block(block_name='complexmixer_2', kind='ccw')  # Rotate CCW complexmixer_2
ctrl.transform_block(block_name='complexmixer_2', kind='ccw')  # Rotate CCW complexmixer_2
ctrl.move_block(block_name='complexmixer_2', dx=0, dy=1)  # Move complexmixer_2
ctrl.transform_block(block_name='complexmixer_2', kind='ccw')  # Rotate CCW complexmixer_2
ctrl.transform_block(block_name='complexmixer_2', kind='mirror_h')  # Mirror H complexmixer_2
ctrl.transform_block(block_name='iqupconvert_2', kind='ccw')  # Rotate CCW iqupconvert_2
ctrl.move_block(block_name='iqupconvert_2', dx=-1, dy=1)  # Move iqupconvert_2
ctrl.transform_block(block_name='complexlowpassfilter_2', kind='ccw')  # Rotate CCW complexlowpassfilter_2
ctrl.move_block(block_name='complexlowpassfilter_2', dx=1, dy=-2)  # Move complexlowpassfilter_2
ctrl.set_route(name='net6', points=[[7, 1], [7, 0], [8, 0]])  # Route connection net6
ctrl.set_route(name='net7', points=[[8, 0], [9, 0]])  # Route connection net7
ctrl.set_route(name='net3', points=[[8, 5], [9, 5], [9, 4], [9, 3], [9, 2], [9, 1], [9, 0]])  # Route connection net3
ctrl.set_route(name='net9', points=[[0, 0], [0, 1]])  # Route connection net9
ctrl.set_route(name='net8', points=[[0, 0], [1, 0], [2, 0], [2, 1], [2, 2], [2, 3], [2, 4], [2, 5], [2, 6], [2, 7], [2, 8], [2, 9], [3, 9], [4, 9], [5, 9], [5, 10]])  # Route connection net8
ctrl.set_route_group(names=['net4', 'net12'], points=[[1, 2], [2, 2], [2, 3], [2, 4], [2, 5], [2, 6], [3, 6]])  # Route connection (I/Q pair)
ctrl.set_route_group(names=['net5', 'net13'], points=[[3, 3], [3, 2], [4, 2], [5, 2]])  # Route connection (I/Q pair)
