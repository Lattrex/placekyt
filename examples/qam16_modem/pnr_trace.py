# placeKYT command trace (runnable replay) — replay with:
#   placekyt --replay this_file.py
# or inside the placeKYT console where `controller` exists:
#   exec(open('this_file.py').read())
ctrl = controller  # the live AppController

ctrl.import_grc(path='examples/qam16_modem/qam16_modem.grc', name='qam16_modem.grc', chip_type='kyttar_10x12')  # Import GNURadio flowgraph qam16_modem.grc (repo-relative; run from the repo root)
ctrl.auto_place(chip=0)  # Auto-place blocks
ctrl.move_block(block_name='qam16complexcostasloop', dx=0, dy=-2)  # Move qam16complexcostasloop
ctrl.transform_block(block_name='mmtimingrecovery', kind='cw')  # Rotate CW mmtimingrecovery
ctrl.transform_block(block_name='mmtimingrecovery', kind='mirror_h')  # Mirror H mmtimingrecovery
ctrl.move_block(block_name='mmtimingrecovery', dx=1, dy=3)  # Move mmtimingrecovery
ctrl.move_block(block_name='qam16slicer', dx=-3, dy=1)  # Move qam16slicer
ctrl.move_block(block_name='qam16complexcostasloop', dx=2, dy=2)  # Move qam16complexcostasloop
ctrl.transform_block(block_name='qam16complexcostasloop', kind='mirror_h')  # Mirror H qam16complexcostasloop
ctrl.move_block(block_name='qam16slicer', dx=2, dy=0)  # Move qam16slicer
ctrl.move_block(block_name='complexgain', dx=0, dy=5)  # Move complexgain
ctrl.move_block(block_name='complexrrcmatchedfilter', dx=0, dy=4)  # Move complexrrcmatchedfilter
ctrl.move_block(block_name='qam16symbolmapper', dx=1, dy=4)  # Move qam16symbolmapper
ctrl.move_block(block_name='complexupsampler', dx=0, dy=4)  # Move complexupsampler
ctrl.move_block(block_name='complexrrcmatchedfilter_2', dx=-2, dy=0)  # Move complexrrcmatchedfilter_2
ctrl.transform_block(block_name='complexrrcmatchedfilter_2', kind='mirror_v')  # Mirror V complexrrcmatchedfilter_2
ctrl.move_block(block_name='complexrrcmatchedfilter_2', dx=-1, dy=2)  # Move complexrrcmatchedfilter_2
ctrl.move_block(block_name='iqupconvert', dx=-2, dy=-2)  # Move iqupconvert
ctrl.move_block(block_name='iqupconvert', dx=1, dy=0)  # Move iqupconvert
ctrl.move_block(block_name='complexrrcmatchedfilter_2', dx=1, dy=0)  # Move complexrrcmatchedfilter_2
ctrl.set_route(name='net7', points=[[0, 0], [0, 1], [0, 2], [0, 3], [0, 4], [0, 5], [0, 6]])  # Route connection net7
ctrl.set_route(name='net9', points=[[0, 0], [0, 1], [0, 2], [0, 3], [0, 4], [0, 5], [1, 5]])  # Route connection net9
ctrl.set_route_group(names=['net4', 'net15'], points=[[1, 7], [1, 8], [1, 9]])  # Route connection (I/Q pair)
ctrl.set_route_group(names=['net1', 'net12'], points=[[1, 9], [2, 9]])  # Route connection (I/Q pair)
ctrl.add_logical_connection(source={'block': 'mmtimingrecovery', 'port': 'yi_e'}, target={'chip': 0, 'port': 'x1_out'}, name='mmtimingrecovery_to_x1_out')  # Add connection mmtimingrecovery_to_x1_out
ctrl.undo()  # Add connection mmtimingrecovery_to_x1_out
ctrl.set_route_group(names=['net5', 'net16'], points=[[8, 11], [9, 11], [9, 10]])  # Route connection (I/Q pair)
ctrl.set_route_group(names=['net2', 'net13'], points=[[8, 7], [7, 7]])  # Route connection (I/Q pair)
ctrl.set_route_group(names=['net3', 'net14'], points=[[3, 5], [3, 4], [2, 4]])  # Route connection (I/Q pair)
ctrl.set_route_group(names=['net10', 'net18'], points=[[2, 4], [1, 4], [1, 3]])  # Route connection (I/Q pair)
ctrl.set_route_group(names=['net6', 'net17'], points=[[2, 2], [2, 1], [2, 0], [3, 0], [4, 0], [5, 0]])  # Route connection (I/Q pair)
ctrl.set_route(name='net11', points=[[7, 1], [7, 2], [8, 2], [9, 2], [9, 1], [9, 0]])  # Route connection net11
ctrl.set_route(name='net8', points=[[7, 5], [8, 5], [9, 5], [9, 4], [9, 3], [9, 2], [9, 1], [9, 0]])  # Route connection net8
