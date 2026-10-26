"""Experimental vendor application startup, semantic control phases.

Capture SHA256 dc6f2ca6b69c88f445f5e07805ad2fd48c12c1e8e601066a91fe72754cf8c4eb.
Only scanner protocol constants are retained. EEPROM responses, diagnostics,
USB descriptors and background interrupt polling are omitted. Register sweep
6935..7511 contains no writes; its read-only diagnostics are not a prerequisite.
All control/bulk writes through final motor disable retain captured order and
pacing, interpreted by the existing guarded Phase executor. Motor status loops
are implemented separately with observed-busy then exact completion checks.
The EEPROM selectors select read windows; no persistent-write claim is made.
"""
from .tables import Op, Phase
from .tables_base import SLOPE_TABLE_LOADER

# Extended registers 0x114/0x115 match vendor configuration values 0x80.
EXTENDED_CONFIG = bytes.fromhex('14801580')
# Full 19-register loader profile, including the first JOG feed.
LOADER_FEED = bytes.fromhex('1c0002183d003e1a3f227d007e757f308a008b008c008d008e008f00900091009200ae00afff')
LOADER_EJECT = bytes.fromhex('1c0002183d003e0c3f127d007e757f308a008b008c008d008e008f00900091009200ae00afff')
TRAVERSE_FEED = bytes.fromhex('021cae00afff3d013e173f42')

CONFIGURE = Phase(
    name="vendor_startup_configure", op_range=(6807, 6934), ops=[
        Op('cr', 0.0, bm=0xc0, br=0x0c, wv=0x008a, wi=0x26fe, length=1, resp=bytes.fromhex('b9')),  # packet 6807
        Op('cw', 0.000987, bm=0x40, br=0x0c, wv=0x008b, wi=0x26fe, data=bytes.fromhex('00')),  # packet 6809
        Op('poll', 0.000922, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6811
        Op('cr', 0.009064, bm=0xc0, br=0x04, wv=0x018e, wi=0x0122, length=2, resp=bytes.fromhex('f055')),  # packet 6813
        Op('cw', 0.106798, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0122027803200402054806180700080009000a480b6c0c000d00110012041308140115801601170c181419001a001b001c001d001e101f00200c2100221a2300')),  # packet 6825
        Op('cr', 0.000843, bm=0xc0, br=0x04, wv=0x008e, wi=0x3722, length=2, resp=bytes.fromhex('3455')),  # packet 6827
        Op('poll', 0.000412, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6829
        Op('cw', 0.000416, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('241a2500260027002b202c122dc030ee3180321f338e348035fb36fc3721386039023a003b003c003d003e003f014f6352075309540b5501560357055a125b00')),  # packet 6831
        Op('poll', 0.000807, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6833
        Op('cw', 0.000388, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('5c005e1f5f07600061006320677f687f690170067109720873097400750076007700780079007a007b007c007d007e757f308000812282008301841885008600')),  # packet 6835
        Op('poll', 0.000868, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6837
        Op('cw', 0.000476, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('87009300940095009d08a009a400a520a600a700a800a900aa00ab30b800b938ba00bd00be00bf00')),  # packet 6839
        Op('poll', 0.000758, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6841
        Op('cw', 0.00045, bm=0x40, br=0x04, wv=0x0183, wi=0x0000, data=EXTENDED_CONFIG),  # packet 6843
        Op('poll', 0.000541, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6845
        Op('cw', 0.000414, bm=0x40, br=0x0c, wv=0x008c, wi=0x0010, data=bytes.fromhex('0d')),  # packet 6847
        Op('poll', 0.000523, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6849
        Op('cw', 0.0004, bm=0x40, br=0x0c, wv=0x008c, wi=0x0013, data=bytes.fromhex('0d')),  # packet 6851
        Op('poll', 0.000533, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6853
        Op('cw', 0.00038, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0b64')),  # packet 6855
        Op('poll', 0.000573, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6857
        Op('cw', 0.000379, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('130f')),  # packet 6859
        Op('poll', 0.000469, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6861
        Op('cw', 0.000403, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0b6c')),  # packet 6863
        Op('poll', 0.001063, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6865
        Op('cw', 0.000434, bm=0x40, br=0x0c, wv=0x008b, wi=0x0007, data=bytes.fromhex('01')),  # packet 6867
        Op('poll', 0.000548, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6869
        Op('cw', 0.000397, bm=0x40, br=0x04, wv=0x008b, wi=0x0009, data=bytes.fromhex('00000000')),  # packet 6871
        Op('poll', 0.00052, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6873
        Op('cr', 0.000399, bm=0xc0, br=0x04, wv=0x008a, wi=0x0010, length=3, resp=bytes.fromhex('c84013')),  # packet 6875
        Op('cw', 0.000724, bm=0x40, br=0x04, wv=0x008b, wi=0x000b, data=bytes.fromhex('00000100')),  # packet 6877
        Op('poll', 0.000513, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6879
        Op('cr', 0.000687, bm=0xc0, br=0x04, wv=0x008a, wi=0x000f, length=64, resp=bytes.fromhex('')),  # packet 6881
        Op('cw', 0.00072, bm=0x40, br=0x04, wv=0x008b, wi=0x000b, data=bytes.fromhex('40000100')),  # packet 6883
        Op('poll', 0.000539, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6885
        Op('cr', 0.000405, bm=0xc0, br=0x04, wv=0x008a, wi=0x000f, length=64, resp=bytes.fromhex('')),  # packet 6887
        Op('cr', 0.000692, bm=0xc0, br=0x04, wv=0x018e, wi=0x0122, length=2, resp=bytes.fromhex('d855')),  # packet 6889
        Op('cr', 0.0005, bm=0xc0, br=0x04, wv=0x018e, wi=0x0122, length=2, resp=bytes.fromhex('d855')),  # packet 6891
        Op('cw', 0.000432, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0310')),  # packet 6893
        Op('poll', 0.00056, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6895
        Op('cw', 0.000411, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0300')),  # packet 6897
        Op('poll', 0.000486, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6899
        Op('cw', 0.00041, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('51005d005ef8')),  # packet 6901
        Op('poll', 0.000469, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6903
        Op('cw', 0.000369, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('51015d005e80')),  # packet 6905
        Op('poll', 0.000472, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6907
        Op('cw', 0.000374, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('51025d005e00')),  # packet 6909
        Op('poll', 0.000442, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6911
        Op('cw', 0.000353, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('51035d005e00')),  # packet 6913
        Op('poll', 0.000437, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6915
        Op('cw', 0.000364, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('51045d005e00')),  # packet 6917
        Op('poll', 0.000512, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6919
        Op('cw', 0.000385, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('51055d005e00')),  # packet 6921
        Op('poll', 0.000487, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6923
        Op('cw', 0.000374, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('51065d005e00')),  # packet 6925
        Op('poll', 0.000572, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6927
        Op('cw', 0.000464, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('51075d005e00')),  # packet 6929
        Op('poll', 0.000494, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 6931
        Op('cr', 0.000355, bm=0xc0, br=0x04, wv=0x018e, wi=0x0122, length=2, resp=bytes.fromhex('d855')),  # packet 6933
    ],
)

TRAVERSE = Phase(
    name="vendor_startup_traverse", op_range=(7513, 7562), ops=[
        Op('cr', 0.003598, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0000, length=1, resp=bytes.fromhex('00')),  # packet 7513
        Op('cr', 0.000455, bm=0xc0, br=0x04, wv=0x008e, wi=0x3122, length=2, resp=bytes.fromhex('fc55')),  # packet 7515
        Op('cw', 0.000409, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('317c')),  # packet 7517
        Op('poll', 0.000472, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 7519
        Op('cr', 0.016986, bm=0xc0, br=0x04, wv=0x008e, wi=0x3122, length=2, resp=bytes.fromhex('7c55')),  # packet 7521
        Op('cw', 0.000771, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('31fc')),  # packet 7523
        Op('poll', 0.000473, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 7525
        Op('cr', 0.000382, bm=0xc0, br=0x04, wv=0x008e, wi=0x3222, length=2, resp=bytes.fromhex('1f55')),  # packet 7527
        Op('cw', 0.000582, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('321d')),  # packet 7529
        Op('poll', 0.000484, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 7531
        Op('poll', 0.000408, bm=0xc0, br=0x04, wv=0x018e, wi=0x0122, length=2, resp=bytes.fromhex('d055')),  # packet 7533
        Op('cr', 0.00038, bm=0xc0, br=0x04, wv=0x018e, wi=0x0022, length=2, resp=bytes.fromhex('7055')),  # packet 7535
        Op('cr', 0.000417, bm=0xc0, br=0x04, wv=0x018e, wi=0x0022, length=2, resp=bytes.fromhex('7055')),  # packet 7537
        Op('cw', 0.000417, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=TRAVERSE_FEED),  # packet 7539
        Op('poll', 0.000575, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 7541
        Op('cw', 0.000779, bm=0x40, br=0x04, wv=0x0082, wi=0x0001, data=bytes.fromhex('00c0001000020000')),  # packet 7543
        Op('poll', 0.000566, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 7545
        Op('bo', 0.000488, data=SLOPE_TABLE_LOADER),  # packet 7547
        Op('poll', 0.000169, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0018, length=1, resp=bytes.fromhex('02')),  # packet 7549
        Op('cw', 0.000431, bm=0x40, br=0x04, wv=0x0082, wi=0x0001, data=bytes.fromhex('0000011000020000')),  # packet 7551
        Op('poll', 0.000504, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 7553
        Op('bo', 0.000456, data=SLOPE_TABLE_LOADER),  # packet 7555
        Op('poll', 0.000143, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0018, length=1, resp=bytes.fromhex('02')),  # packet 7557
        Op('cw', 0.000466, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0f01')),  # packet 7559
        Op('poll', 0.000504, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 7561
    ],
)

JOG_FEED_FIRST = Phase(
    name="vendor_startup_jog_feed_first", op_range=(9219, 9272), ops=[
        Op('cw', 0.01397, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('36fc')),  # packet 9219
        Op('poll', 0.000696, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9223
        Op('cw', 0.000402, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('338e')),  # packet 9225
        Op('poll', 0.000658, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9227
        Op('cr', 0.000649, bm=0xc0, br=0x04, wv=0x008e, wi=0x3222, length=2, resp=bytes.fromhex('0555')),  # packet 9229
        Op('cw', 0.007967, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('3207')),  # packet 9233
        Op('poll', 0.000903, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9235
        Op('cr', 0.000438, bm=0xc0, br=0x04, wv=0x018e, wi=0x0122, length=2, resp=bytes.fromhex('d855')),  # packet 9237
        Op('cw', 0.000435, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0908')),  # packet 9239
        Op('poll', 0.000652, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9241
        Op('cw', 0.000924, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=LOADER_FEED),  # packet 9243
        Op('poll', 0.007911, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9247
        Op('cw', 0.000958, bm=0x40, br=0x04, wv=0x0082, wi=0x0001, data=bytes.fromhex('00c0001000020000')),  # packet 9251
        Op('poll', 0.000617, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9253
        Op('bo', 0.000616, data=SLOPE_TABLE_LOADER),  # packet 9255
        Op('poll', 0.000309, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0018, length=1, resp=bytes.fromhex('02')),  # packet 9257
        Op('cw', 0.000424, bm=0x40, br=0x04, wv=0x0082, wi=0x0001, data=bytes.fromhex('0000011000020000')),  # packet 9259
        Op('poll', 0.000689, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9261
        Op('bo', 0.000479, data=SLOPE_TABLE_LOADER),  # packet 9263
        Op('poll', 0.000126, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0018, length=1, resp=bytes.fromhex('02')),  # packet 9265
        Op('cw', 0.000418, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0f01')),  # packet 9267
        Op('poll', 0.007366, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9271
    ],
)

JOG_FEED_SECOND = Phase(
    name="vendor_startup_jog_feed_second", op_range=(9507, 9562), ops=[
        Op('cw', 0.015772, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0900')),  # packet 9507
        Op('poll', 0.000902, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9509
        Op('cr', 0.000712, bm=0xc0, br=0x04, wv=0x008e, wi=0x3222, length=2, resp=bytes.fromhex('1f55')),  # packet 9511
        Op('cw', 0.000876, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('321f')),  # packet 9513
        Op('poll', 0.001008, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9515
        Op('cr', 0.000818, bm=0xc0, br=0x04, wv=0x008e, wi=0x3522, length=2, resp=bytes.fromhex('fb55')),  # packet 9517
        Op('cw', 0.000726, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('35bb')),  # packet 9519
        Op('poll', 0.000836, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9521
        Op('cr', 0.000773, bm=0xc0, br=0x04, wv=0x008e, wi=0x3222, length=2, resp=bytes.fromhex('1f55')),  # packet 9523
        Op('cr', 0.008564, bm=0xc0, br=0x04, wv=0x018e, wi=0x0122, length=2, resp=bytes.fromhex('f855')),  # packet 9525
        Op('cr', 0.506708, bm=0xc0, br=0x04, wv=0x018e, wi=0x0122, length=2, resp=bytes.fromhex('f855')),  # packet 9527
        Op('cr', 0.000872, bm=0xc0, br=0x04, wv=0x008e, wi=0x3222, length=2, resp=bytes.fromhex('1f55')),  # packet 9529
        Op('cw', 0.000577, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('321d')),  # packet 9531
        Op('poll', 0.000628, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9533
        Op('cr', 0.000507, bm=0xc0, br=0x04, wv=0x018e, wi=0x0022, length=2, resp=bytes.fromhex('f055')),  # packet 9535
        Op('cr', 0.000521, bm=0xc0, br=0x04, wv=0x018e, wi=0x0122, length=2, resp=bytes.fromhex('f855')),  # packet 9537
        Op('cw', 0.000525, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=LOADER_FEED),  # packet 9539
        Op('poll', 0.000812, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9541
        Op('cw', 0.000633, bm=0x40, br=0x04, wv=0x0082, wi=0x0001, data=bytes.fromhex('00c0001000020000')),  # packet 9543
        Op('poll', 0.000591, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9545
        Op('bo', 0.000526, data=SLOPE_TABLE_LOADER),  # packet 9547
        Op('poll', 0.000227, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0018, length=1, resp=bytes.fromhex('02')),  # packet 9549
        Op('cw', 0.000541, bm=0x40, br=0x04, wv=0x0082, wi=0x0001, data=bytes.fromhex('0000011000020000')),  # packet 9551
        Op('poll', 0.000593, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9553
        Op('bo', 0.000541, data=SLOPE_TABLE_LOADER),  # packet 9555
        Op('poll', 0.000245, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0018, length=1, resp=bytes.fromhex('02')),  # packet 9557
        Op('cw', 0.000444, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0f01')),  # packet 9559
        Op('poll', 0.000515, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9561
    ],
)

JOG_EJECT = Phase(
    name="vendor_startup_jog_eject", op_range=(9795, 9828), ops=[
        Op('cr', 0.000719, bm=0xc0, br=0x04, wv=0x008e, wi=0x3222, length=2, resp=bytes.fromhex('1d55')),  # packet 9795
        Op('cw', 0.000718, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('321f')),  # packet 9797
        Op('poll', 0.001082, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9799
        Op('cw', 0.000814, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0908')),  # packet 9801
        Op('poll', 0.001, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9803
        Op('cw', 0.000857, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=LOADER_EJECT),  # packet 9805
        Op('poll', 0.001361, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9807
        Op('cw', 0.00092, bm=0x40, br=0x04, wv=0x0082, wi=0x0001, data=bytes.fromhex('00c0001000020000')),  # packet 9809
        Op('poll', 0.0009, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9811
        Op('bo', 0.000847, data=SLOPE_TABLE_LOADER),  # packet 9813
        Op('poll', 0.000466, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0018, length=1, resp=bytes.fromhex('02')),  # packet 9815
        Op('cw', 0.000824, bm=0x40, br=0x04, wv=0x0082, wi=0x0001, data=bytes.fromhex('0000011000020000')),  # packet 9817
        Op('poll', 0.000935, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9819
        Op('bo', 0.000849, data=SLOPE_TABLE_LOADER),  # packet 9821
        Op('poll', 0.000427, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0018, length=1, resp=bytes.fromhex('02')),  # packet 9823
        Op('cw', 0.000537, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0f01')),  # packet 9825
        Op('poll', 0.000868, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9827
    ],
)

DISABLE = Phase(
    name="vendor_startup_disable", op_range=(9943, 9946), ops=[
        Op('cw', 0.015827, bm=0x40, br=0x04, wv=0x0083, wi=0x0000, data=bytes.fromhex('0900')),  # packet 9943
        Op('poll', 0.0012, bm=0xc0, br=0x0c, wv=0x008e, wi=0x0020, length=1, resp=bytes.fromhex('55')),  # packet 9945
    ],
)

# Completion budgets: ceil(3 * 6.8873) = 21 seconds for traverse;
# JOGs retain 3 times their measured GO-to-done durations.
MOTOR_PHASES = (
    (TRAVERSE, frozenset((b'\xd1\x55', b'\xd9\x55')), b'\xd8\x55', 21.0),
    (JOG_FEED_FIRST, frozenset((b'\xd9\x55', b'\xf9\x55')), b'\xf8\x55', 3 * 1.6047),
    (JOG_FEED_SECOND, frozenset((b'\xd9\x55', b'\xf9\x55')), b'\xf8\x55', 3 * 1.6047),
    (JOG_EJECT, frozenset((b'\xd9\x55', b'\xf9\x55')), b'\xf8\x55', 3 * 0.8890),
)
