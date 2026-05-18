#!/usr/bin/env python3
"""
世界观图片批量生成器
使用10个并行agent生成建筑、场景、仙境、妖兽、武器各10种
"""
import subprocess
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_DIR = Path("D:/AiProject/Node/projects/novels7")
IMAGES_DIR = PROJECT_DIR / "images"
LOG_FILE = PROJECT_DIR / "logs/world_images.log"

CATEGORIES = {
    "buildings": [
        ("yunlanzong_gate", "Grand Chinese fantasy xianxia mountain sect gate, massive stone archway with golden calligraphy, thousand stone stairs ascending into clouds, ancient pagodas on cliff sides, ethereal mist, sunrise lighting, epic scale architecture, highly detailed, 4K"),
        ("ancient_ruins", "Ancient Chinese cultivator ruins hidden in mountain cave, crumbling stone pillars with glowing runes, broken statues, moss-covered altar with mysterious jade tablet, dim blue magical light, mysterious atmosphere, highly detailed, 4K"),
        ("reincarnation_hall", "Majestic Chinese fantasy Reincarnation Hall, circular building with black obsidian walls, floating soul lanterns, bridge over river of stars, ethereal spirits wandering, dark mystical atmosphere with purple glow, highly detailed, 4K"),
        ("xiao_family_mansion", "Ancient Chinese noble family compound, traditional courtyard architecture with red pillars, golden roof tiles, stone lion statues at gate, blooming cherry trees, luxury and power atmosphere, highly detailed, 4K"),
        ("tiandao_sect_hall", "Heavenly Chinese fantasy sect main hall, white jade palace floating in clouds, golden dragon pillars, celestial light beams, immortals gathering, divine and imposing architecture, highly detailed, 4K"),
        ("devil_abyss_entrance", "Dark ominous entrance to the Devil Abyss forbidden land, cracked black stone gate with blood-red runes, swirling dark miasma, dead trees, skulls scattered, horror atmosphere, highly detailed, 4K"),
        ("lin_yuan_cottage", "Humble Chinese village cottage, thatched roof wooden house, simple vegetable garden, mountains in background, peaceful rural scene, warm sunset lighting, humble but cozy, highly detailed, 4K"),
        ("immortal_king_palace", "Magnificent Chinese fantasy immortal king palace, crystal towers reaching sky, rainbow bridges, phoenix statues, clouds surrounding the throne room, opulent divine architecture, highly detailed, 4K"),
        ("void_rift", "Cosmic dimensional rift in sky, swirling vortex of purple and black energy, shattered space fragments floating, stars visible through crack, apocalyptic atmosphere, highly detailed, 4K"),
        ("cultivation_market", "Bustling Chinese fantasy cultivation market street, ancient wooden stalls selling magical herbs and talismans, cultivators in various robes bargaining, lanterns hanging, lively atmosphere, highly detailed, 4K"),
    ],
    "scenes": [
        ("village_hometown", "Peaceful Chinese ancient mountain village, small wooden houses with smoke rising, terraced fields, river flowing through, children playing, warm nostalgic atmosphere, highly detailed landscape, 4K"),
        ("dark_forest", "Dark mysterious Chinese fantasy forest, ancient twisted trees with hanging vines, fog covering ground, glowing eyes in shadows, eerie blue moonlight filtering through canopy, dangerous atmosphere, highly detailed, 4K"),
        ("bloody_battlefield", "Epic Chinese fantasy battlefield aftermath, scorched earth with craters, broken weapons scattered, blood-red sky, smoke rising, crows circling, tragic and epic atmosphere, highly detailed, 4K"),
        ("spirit_world_entrance", "Magical portal to the Spirit World, swirling golden gateway between two stone pillars, spirit energy flowing out, rainbow light, mystical runes floating, transcendent atmosphere, highly detailed, 4K"),
        ("cliff_waterfall", "Dramatic Chinese mountain cliff with massive waterfall, water crashing into misty pool below, rainbow in spray, lone cultivator meditating on ledge, epic nature scenery, highly detailed, 4K"),
        ("desert_ancient_city", "Ruins of ancient Chinese city in vast desert, sand-covered stone walls, broken pagodas, desert wind blowing sand, golden sunset, desolate mysterious atmosphere, highly detailed, 4K"),
        ("dragon_palace_undersea", "Magnificent Chinese underwater dragon palace, crystal corridors, glowing pearls illuminating, coral gardens, sea creatures swimming, translucent blue water atmosphere, highly detailed, 4K"),
        ("volcano_lava_cave", "Dangerous volcanic lava cave interior, river of molten lava flowing, obsidian stalactites, fire demons lurking, intense orange-red lighting, extreme heat atmosphere, highly detailed, 4K"),
        ("ancient_star_path", "Cosmic ancient star path in space, floating stone steps leading through nebula, stars and galaxies around, cultivator walking the path, vast and lonely universe, highly detailed, 4K"),
        ("frozen_snowfield", "Vast frozen Chinese fantasy snowfield, endless white snow, ice crystals reflecting light, aurora in sky, frozen ancient statues buried in ice, cold desolate beauty, highly detailed, 4K"),
    ],
    "fairylands": [
        ("nine_heavens", "Chinese fantasy Nine Heavens realm, layers of floating celestial palaces above clouds, golden bridges connecting islands, immortal cranes flying, divine light, supreme heavenly atmosphere, highly detailed, 4K"),
        ("penglai_island", "Mythical Penglai fairy island floating in sea of clouds, ancient peach trees bearing immortal fruit, white deer grazing, pavilion on cliff edge, ethereal paradise atmosphere, highly detailed, 4K"),
        ("jade_pond", "Heavenly Jade Pool fairyland, crystal clear turquoise water, lotus flowers blooming, celestial maidens bathing, rainbow mist, jade pavilions reflected in water, divine beauty, highly detailed, 4K"),
        ("kunlun_mountain", "Sacred Kunlun mountain peak above clouds, rainbow bridge to heaven, divine palace at summit, auspicious clouds, golden light shining, holy and majestic atmosphere, highly detailed, 4K"),
        ("peach_blossom_valley", "Idyllic Chinese peach blossom valley, thousands of pink peach trees in full bloom, stream flowing through, wooden bridge, misty mountains background, utopian peaceful atmosphere, highly detailed, 4K"),
        ("purple_bamboo_grove", "Serene purple bamboo forest fairyland, tall purple bamboo stalks swaying, morning mist, stone path winding through, small meditation hut, zen tranquil atmosphere, highly detailed, 4K"),
        ("milky_way_waterfall", "Magical waterfall flowing from Milky Way sky, silver water falling from stars, rainbow at base, celestial energy particles, dreamlike impossible landscape, highly detailed, 4K"),
        ("cloud_sea_sunrise", "Breathtaking sea of clouds at sunrise, golden sun rising above cloud ocean, mountain peaks emerging like islands, golden light illuminating everything, majestic divine scenery, highly detailed, 4K"),
        ("immortal_herb_valley", "Hidden valley of immortal herbs, glowing magical plants of various colors, rainbow mist, rare spirit flowers, crystal dew drops, treasure land atmosphere, highly detailed, 4K"),
        ("yuxu_palace", "Grand Yuxu Palace in heavenly realm, pure white jade construction, golden roof, surrounded by divine light, immortals flying around, most sacred place atmosphere, highly detailed, 4K"),
    ],
    "monsters": [
        ("nine_head_serpent", "Giant nine-headed demonic serpent beast, each head with glowing red eyes and venomous fangs, dark scales, surrounded by poison mist, emerging from swamp, terrifying Chinese fantasy monster, highly detailed, 4K"),
        ("fire_kylin", "Majestic fire qilin mythical beast, body covered in crimson flames, golden horns, dragon-like scales, hooves leaving fire prints, noble and powerful stance, Chinese fantasy creature, highly detailed, 4K"),
        ("frost_dragon", "Ancient ice dragon coiled around mountain peak, translucent blue crystal scales, frost breath visible, piercing blue eyes, snow swirling around, majestic and deadly, highly detailed, 4K"),
        ("soul_devouring_wolf", "Demonic soul-devouring wolf with three eyes, dark fur with purple flame patterns, fangs dripping dark energy, standing on pile of bones, menacing and supernatural, highly detailed, 4K"),
        ("golden_wing_peng", "Massive golden-winged roc bird with wingspan covering mountains, golden feathers gleaming, sharp talons, flying through clouds, divine bird of legend, majestic and powerful, highly detailed, 4K"),
        ("mystic_turtle", "Ancient black tortoise xuanwu beast the size of a hill, snake coiled on its back, shell covered in mystical runes, deep wise eyes, earth-shaking presence, highly detailed, 4K"),
        ("blood_eye_spider", "Giant demonic spider with blood-red compound eyes, body covered in black spiky hairs, webs made of dark energy, hanging in cave, hundreds of eyes glowing, horror atmosphere, highly detailed, 4K"),
        ("ghost_tiger", "Skeletal ghost tiger with translucent body, burning blue ghost flames, fangs visible through decaying flesh, prowling in graveyard, undead horror beast, highly detailed, 4K"),
        ("thunder_leopard", "Lightning-fast thunder leopard beast, body crackling with electric arcs, silver-blue fur standing on end, eyes like lightning bolts, mid-leap through storm clouds, dynamic and powerful, highly detailed, 4K"),
        ("void_tentacle", "Cosmic horror void tentacle monster emerging from dimensional tear, countless writhing dark tentacles with eyes, reality distorting around it, Lovecraftian xianxia fusion, highly detailed, 4K"),
    ],
    "weapons": [
        ("nitiandao_sword", "Legendary Chinese fantasy xianxia sword named NiTian, blade with flowing golden runes, hilt wrapped in dragon leather, floating in air with golden aura, ancient divine weapon, highly detailed, 4K"),
        ("karma_wheel", "Mystical Chinese fantasy Karma Wheel artifact, rotating golden disc with complex engravings, threads of fate visible, floating with ethereal glow, cosmic divination tool, highly detailed, 4K"),
        ("reincarnation_seal", "Ancient Chinese Reincarnation Seal artifact, jade stamp with dragon and phoenix carving, glowing with purple soul energy, stamps leave golden marks, mystical soul tool, highly detailed, 4K"),
        ("blood_artifact", "Evil Chinese fantasy Blood God artifact, crimson jade pendant dripping blood essence, dark red glow, skull motifs, corrupted magical aura, demonic cultivation tool, highly detailed, 4K"),
        ("heavenly_order", "Divine Chinese fantasy Heavenly Order token, pure white jade tablet with golden heavenly script, radiating divine authority light, command of heaven itself, highly detailed, 4K"),
        ("demon_slaying_blade", "Massive Chinese fantasy demon-slaying blade, black steel with red edge, dragon-shaped guard, blood grooves, standing embedded in rock, killing intent visible, highly detailed, 4K"),
        ("universe_bag", "Mystical Chinese fantasy Universe Storage Bag, embroidered silk pouch with space distortion effect, stars visible inside opening, bottomless dimensional storage artifact, highly detailed, 4K"),
        ("spirit_flying_sword", "Elegant Chinese flying sword for spirit control, slender transparent crystal blade, hilt with jade orb, hovering with blue spiritual energy trail, graceful magical weapon, highly detailed, 4K"),
        ("soul_suppressing_bell", "Ancient Chinese Soul Suppressing Bell, bronze bell covered in exorcism talismans, golden energy waves emanating, sound visible as shockwaves, spiritual suppression artifact, highly detailed, 4K"),
        ("void_blade", "Cosmic Chinese fantasy Void Blade weapon, blade made of condensed space itself, reality warping around edge, purple-black energy, can cut through dimensions, legendary space weapon, highly detailed, 4K"),
    ],
}


def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def generate_image(task):
    """生成单张图片"""
    category, name, prompt = task
    out_dir = IMAGES_DIR / category
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "mmx", "image", "generate",
        "--prompt", prompt,
        "--out-dir", str(out_dir),
        "--out-prefix", name,
        "--quiet",
    ]

    start = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, shell=True)
        elapsed = time.time() - start
        if result.returncode == 0:
            log(f"[OK] {category}/{name} 完成 ({elapsed:.1f}s)")
            return category, name, "success", elapsed
        else:
            err = result.stderr[-100:] if result.stderr else "unknown"
            log(f"[FAIL] {category}/{name}: {err}")
            return category, name, "failed", elapsed
    except subprocess.TimeoutExpired:
        log(f"[TIMEOUT] {category}/{name}")
        return category, name, "timeout", 120
    except Exception as e:
        log(f"[ERROR] {category}/{name}: {e}")
        return category, name, "error", 0


def main():
    print("=" * 60)
    print("世界观图片批量生成器启动")
    print("=" * 60)

    # 构建任务列表
    tasks = []
    for category, items in CATEGORIES.items():
        for name, prompt in items:
            tasks.append((category, name, prompt))

    print(f"总计: {len(tasks)} 张图片, 10个并行agent")

    total_success = 0
    total_failed = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(generate_image, task): task for task in tasks}
        for future in as_completed(futures):
            category, name, status, elapsed = future.result()
            if status == "success":
                total_success += 1
            else:
                total_failed += 1
            print(f"进度: {total_success}/{len(tasks)} 完成, 失败: {total_failed}")

    elapsed_total = time.time() - start_time
    log("=" * 60)
    log(f"全部完成! 成功: {total_success}, 失败: {total_failed}, 耗时: {elapsed_total/60:.1f}分钟")
    log("=" * 60)


if __name__ == "__main__":
    main()
