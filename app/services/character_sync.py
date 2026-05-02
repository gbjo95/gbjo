from __future__ import annotations

from typing import Any

from app.database.connection import get_db
from app.services.discord_service import discord_service
from app.services.lostark_service import parse_float, search_lostark_character


_DEFAULT_CLASS_EMOJI = '⚔️'


async def ensure_class_row(class_name: str | None, emoji: str | None = None) -> dict[str, Any] | None:
    normalized = (class_name or '').strip()
    if not normalized:
        return None
    async with get_db() as db:
        rows = await db.execute(
            'SELECT id, name, emoji FROM class WHERE name = ? LIMIT 1',
            (normalized,),
        )
        if rows:
            return rows[0]
        await db.execute(
            'INSERT INTO class (name, emoji) VALUES (?, ?)',
            (normalized, (emoji or _DEFAULT_CLASS_EMOJI)),
        )
        class_id = int(db.lastrowid or 0)
        await db.commit()
        rows = await db.execute(
            'SELECT id, name, emoji FROM class WHERE id = ? LIMIT 1',
            (class_id,),
        )
        return rows[0] if rows else {'id': class_id, 'name': normalized, 'emoji': (emoji or _DEFAULT_CLASS_EMOJI)}


async def save_character_to_db(char_data: dict[str, Any], user_id: str = '') -> dict[str, Any] | None:
    try:
        async with get_db() as db:
            combined_result = await db.execute(
                '''SELECT cl.id AS class_id, cl.emoji AS class_emoji,
                          c.id AS existing_id, c.combat_power AS existing_combat_power,
                          c.item_lvl AS existing_item_lvl
                   FROM class cl
                   LEFT JOIN character c ON c.char_name = ? AND c.class_id = cl.id
                   WHERE cl.name = ?
                   LIMIT 1''',
                ((char_data.get('char_name') or '').strip(), (char_data.get('class_name') or '').strip()),
            )
            if not combined_result:
                return None
            result = combined_result[0]
            class_id = int(result['class_id'])
            class_emoji = result.get('class_emoji') or _DEFAULT_CLASS_EMOJI
            existing_id = int(result.get('existing_id') or 0)
            existing_combat_power = parse_float(result.get('existing_combat_power'))
            existing_item_lvl = parse_float(result.get('existing_item_lvl'))
            new_item_lvl = parse_float(char_data.get('item_lvl'))
            new_combat_power = parse_float(char_data.get('combat_power'))
            char_name = (char_data.get('char_name') or '').strip()
            class_name = (char_data.get('class_name') or '').strip()
            if not char_name or not class_name:
                return None
            if existing_id:
                update_fields: list[str] = []
                params: list[Any] = []
                if new_item_lvl != existing_item_lvl:
                    update_fields.append('item_lvl = ?')
                    params.append(new_item_lvl)
                if new_combat_power > existing_combat_power:
                    update_fields.append('combat_power = ?')
                    params.append(new_combat_power)
                if update_fields:
                    update_fields.append('class_id = ?')
                    params.append(class_id)
                    if user_id:
                        update_fields.append('user_id = COALESCE(user_id, ?)')
                        params.append(str(user_id))
                    update_fields.append('updated_at = CURRENT_TIMESTAMP')
                    params.append(existing_id)
                    await db.execute(f"UPDATE character SET {', '.join(update_fields)} WHERE id = ?", tuple(params))
                elif user_id:
                    await db.execute('UPDATE character SET user_id = COALESCE(user_id, ?) WHERE id = ?', (str(user_id), existing_id))
                char_id = existing_id
                final_combat_power = max(existing_combat_power, new_combat_power)
            else:
                await db.execute(
                    'INSERT INTO character (user_id, class_id, char_name, item_lvl, combat_power, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
                    (str(user_id) if user_id else None, class_id, char_name, new_item_lvl, new_combat_power),
                )
                char_id = int(db.lastrowid or 0)
                final_combat_power = new_combat_power
            if not char_id:
                await db.rollback()
                return None
            if user_id:
                await db.execute('INSERT OR IGNORE INTO siblings (user_id, character_id) VALUES (?, ?)', (str(user_id), char_id))
            await db.commit()
            return {
                'id': char_id,
                'user_id': str(user_id),
                'class_id': class_id,
                'char_name': char_name,
                'item_lvl': new_item_lvl,
                'combat_power': final_combat_power,
                'class_name': class_name,
                'class_emoji': class_emoji,
            }
    except Exception:
        return None


async def get_party_ids_for_character_ids(character_ids: list[int]) -> list[int]:
    if not character_ids:
        return []
    async with get_db() as db:
        placeholders = ','.join('?' for _ in character_ids)
        rows = await db.execute(
            f'''SELECT DISTINCT p.id
                FROM party p
                JOIN participants pt ON p.id = pt.party_id
                WHERE pt.character_id IN ({placeholders})
                  AND p.thread_manage_id IS NOT NULL''',
            character_ids,
        ) or []
    return [int(row['id']) for row in rows]


async def batch_update_discord_posts(party_ids: list[int]) -> dict[str, Any]:
    if not party_ids:
        return {'success': [], 'failed': [], 'total': 0}
    results: dict[str, Any] = {'success': [], 'failed': [], 'total': len(party_ids)}
    async with get_db() as db:
        placeholders = ','.join('?' for _ in party_ids)
        parties_data = await db.execute(
            f'''SELECT p.id, p.title, p.thread_manage_id, p.owner, p.message,
                      p.is_dealer_closed, p.is_supporter_closed,
                      r.name AS raid_name, r.difficulty, r.dealer, r.supporter
               FROM party p
               LEFT JOIN raid r ON p.raid_id = r.id
               WHERE p.id IN ({placeholders}) AND p.thread_manage_id IS NOT NULL''',
            party_ids,
        ) or []
        if not parties_data:
            return results
        party_id_set = [int(p['id']) for p in parties_data]
        placeholders2 = ','.join('?' for _ in party_id_set)
        participants_rows = await db.execute(
            f'''SELECT p.party_id, p.user_id, p.role,
                      c.char_name, c.item_lvl, c.combat_power,
                      cl.name AS class_name, cl.emoji AS class_emoji
               FROM participants p
               LEFT JOIN character c ON p.character_id = c.id
               LEFT JOIN class cl ON c.class_id = cl.id
               WHERE p.party_id IN ({placeholders2})
               ORDER BY p.joined_at ASC''',
            party_id_set,
        ) or []
    grouped: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for row in participants_rows:
        pid = int(row['party_id'])
        g = grouped.get(pid)
        if g is None:
            g = {'dealers': [], 'supporters': []}
            grouped[pid] = g
        data = {
            'user_id': row.get('user_id'),
            'name': row.get('char_name'),
            'item_level': row.get('item_lvl'),
            'class_name': row.get('class_name'),
            'class_emoji': row.get('class_emoji'),
            'combat_power': row.get('combat_power'),
        }
        if int(row.get('role') or 0) == 0:
            g['dealers'].append(data)
        else:
            g['supporters'].append(data)
    for party in parties_data:
        pid = int(party['id'])
        try:
            success = await discord_service.update_forum_post(
                str(party['thread_manage_id']),
                {
                    **party,
                    'participants': grouped.get(pid, {'dealers': [], 'supporters': []}),
                },
            )
            if success:
                results['success'].append(pid)
            else:
                results['failed'].append({'party_id': pid, 'reason': 'Discord API failed'})
        except Exception as e:
            results['failed'].append({'party_id': pid, 'reason': str(e)})
    return results
