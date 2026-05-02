raid_meta = {
    '4막': {
        '노말': {'min_lvl': 1700, 'dealer': 6, 'supporter': 2},
        '하드': {'min_lvl': 1720, 'dealer': 6, 'supporter': 2},
    },
    '종막': {
        '노말': {'min_lvl': 1710, 'dealer': 6, 'supporter': 2},
        '하드': {'min_lvl': 1730, 'dealer': 6, 'supporter': 2},
    },
    '세르카': {
        '노말': {'min_lvl': 1700, 'dealer': 3, 'supporter': 1},
        '하드': {'min_lvl': 1730, 'dealer': 3, 'supporter': 1},
        '나메': {'min_lvl': 1740, 'dealer': 3, 'supporter': 1},        
    },
    '지평': {
        '1단계': {'min_lvl': 1700, 'dealer': 3, 'supporter': 1},
        '2단계': {'min_lvl': 1720, 'dealer': 3, 'supporter': 1},
        '3단계': {'min_lvl': 1750, 'dealer': 3, 'supporter': 1},    
    },
    'EX에기르': {
        '노말': {'min_lvl': 1720, 'dealer': 6, 'supporter': 2},
        '하드': {'min_lvl': 1750, 'dealer': 6, 'supporter': 2},
        '나메': {'min_lvl': 1770, 'dealer': 6, 'supporter': 2},    
    },
    'EX아브': {
        '노말': {'min_lvl': 1720, 'dealer': 6, 'supporter': 2},
        '하드': {'min_lvl': 1750, 'dealer': 6, 'supporter': 2},
        '나메': {'min_lvl': 1770, 'dealer': 6, 'supporter': 2},       
    },
}

raid_difficulty_map = {k: list(v.keys()) for k, v in raid_meta.items()}
raid_list = list(raid_meta.keys())
