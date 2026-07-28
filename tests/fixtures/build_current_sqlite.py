"""Build a synthetic, PII-free current-schema SQLite source for migration CI."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(args.data_dir.resolve())
    os.environ["BOT_TOKEN"] = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    os.environ["ADMIN_IDS"] = ""
    os.environ["WITHDRAW_ACCOUNT_KEY"] = Fernet.generate_key().decode("ascii")
    os.environ["TELEGRAM_INBOX_KEY"] = Fernet.generate_key().decode("ascii")
    os.environ["HEALTH_TOKEN"] = "health_" + "h" * 40
    os.environ["MEDIA_SIGNING_KEY"] = "media_" + "m" * 40

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import main as application
    from db_migration.access_contract import CAPABILITIES_V1

    asyncio.run(application.init_db())
    stamp = application.now_iso()
    update_payload = json.dumps(
        {"update_id": 99001}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    )
    with sqlite3.connect(application.DB_PATH) as db:
        db.execute(
            "INSERT INTO members "
            "(user_id,full_name,city,role,status,bonus,done_count,referred_by,"
            "created_at,approved_at,approved_by,applied_at,chat_xp,ref_confirmed) "
            "VALUES (101,'Администратор','Краснодар','admin','approved',0,0,NULL,"
            "?,?,NULL,?,0,0)", (stamp, stamp, stamp),
        )
        db.execute(
            "INSERT INTO members "
            "(user_id,full_name,city,help_type,about,role,status,bonus,done_count,"
            "referred_by,created_at,approved_at,approved_by,applied_at,chat_xp,ref_confirmed) "
            "VALUES (102,'Исполнитель','Краснодар','Парковки','Тест fixture',"
            "'helper','approved',60,1,101,?,?,?,?,0,1)",
            (stamp, stamp, 101, stamp),
        )
        db.execute(
            "INSERT INTO members "
            "(user_id,full_name,city,role,status,bonus,done_count,referred_by,"
            "created_at,approved_at,approved_by,applied_at,chat_xp,ref_confirmed) "
            "VALUES (103,'Второй администратор','Краснодар','admin','approved',0,0,NULL,"
            "?,?,101,?,0,0)", (stamp, stamp, stamp),
        )
        db.execute(
            "INSERT INTO admin_authorities "
            "(user_id,origin,granted_operation_id,granted_at) "
            "VALUES (101,'manual','fixture-authority',?)", (stamp,),
        )
        grant_id = db.execute(
            "INSERT INTO staff_access_grants "
            "(id,user_id,preset,origin,status,policy_version,generation,"
            "grant_operation_id,granted_at) "
            "VALUES (9001,101,'owner','manual','active',1,1,"
            "'rbac-v1-backfill:101:manual:fixture-authority',?)",
            (stamp,),
        ).lastrowid
        db.executemany(
            "INSERT INTO staff_grant_capabilities (grant_id,capability) VALUES (?,?)",
            [(grant_id, capability) for capability in CAPABILITIES_V1],
        )
        db.execute(
            "INSERT INTO staff_access_changes "
            "(id,target_user_id,change_action,preset,expected_generation,reason,status,"
            "requested_by,requested_at,request_operation_id,request_hash) "
            "VALUES (9101,102,'assign','scout',0,'Тестовый доступ','pending',"
            "101,?,'access-request-fixture',?)",
            (stamp, "4" * 64),
        )
        owner_snapshot = json.dumps({
            "capabilities": sorted(CAPABILITIES_V1),
            "generation": 1,
            "origin": "manual",
            "policy_version": 1,
            "preset": "owner",
            "status": "active",
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        db.execute(
            "INSERT INTO staff_access_events "
            "(id,target_user_id,preset,event_type,actor_id,operation_id,"
            "policy_version,before_json,after_json,created_at) "
            "VALUES (9201,101,'owner','assign',NULL,"
            "'rbac-v1-event:101:manual:1',1,'{}',?,?)",
            (owner_snapshot, stamp),
        )
        db.execute(
            "INSERT INTO telegram_join_requests "
            "(request_key,update_id,chat_id,user_id,invite_link_sha256,source,status,"
            "requested_at,decision,decision_queued_at,manual_retry_reason,"
            "manual_retry_by,manual_retry_at) VALUES (?,?,?,?,?,'bot_invite',"
            "'approve_queued',?,'approve',?,?,?,?)",
            (
                "j" * 64, 99002, "-1001111111111", 102, "a" * 64,
                stamp, stamp, "Проверенный повтор", 101, stamp,
            ),
        )
        db.execute(
            "INSERT INTO media_objects "
            "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
            "upload_operation_id,request_hash,created_at,ready_at,reconcile_attempts) "
            "VALUES ('media-fixture','local','media-fixture.jpg','task_proof','ready',"
            "'image/jpeg',4,?,'media-upload-fixture','media-request-fixture',?,?,0)",
            ("a" * 64, stamp, stamp),
        )
        template_media_sha = "b" * 64
        db.execute(
            "INSERT INTO media_objects "
            "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
            "upload_operation_id,request_hash,created_at,ready_at,reconcile_attempts) "
            "VALUES ('media-template-fixture','local','template-fixture.jpg',"
            "'task_template_brief','ready','image/jpeg',4,?,"
            "'media-template-upload-fixture','media-template-request-fixture',?,?,0)",
            (template_media_sha, stamp, stamp),
        )
        template_id = "80c3f0b4-44fd-4df6-866e-d9872e7aa874"
        template_version_id = "d05974dc-2379-4ca5-b049-e862f5938f40"
        template_content = {
            "title": "Fixture template",
            "task_type": "fix_zone",
            "task_title": "Fixture versioned task",
            "details": "Synthetic migration template",
            "reward": 90,
            "mode": "open",
            "evidence_policy": "photo_required",
            "max_participants": 1,
            "budget_cap": 90,
            "photo_media_id": "media-template-fixture",
            "photo_sha256": template_media_sha,
        }
        template_content_hash = hashlib.sha256(json.dumps(
            template_content, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        db.execute(
            "INSERT INTO task_templates "
            "(id,key,origin,status,generation,current_version_id,created_by,"
            "created_at,updated_by,updated_at) VALUES "
            "(?, 'fixture_template','manual','active',1,?,101,?,101,?)",
            (template_id, template_version_id, stamp, stamp),
        )
        db.execute(
            "INSERT INTO task_template_versions "
            "(id,template_id,version_number,title,task_type,task_title,details,"
            "reward,mode,evidence_policy,max_participants,budget_cap,photo_media_id,"
            "photo_sha256,content_hash,created_by,created_at) "
            "VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,101,?)",
            (
                template_version_id, template_id, template_content["title"],
                template_content["task_type"], template_content["task_title"],
                template_content["details"], template_content["reward"],
                template_content["mode"], template_content["evidence_policy"],
                template_content["max_participants"], template_content["budget_cap"],
                template_content["photo_media_id"], template_content["photo_sha256"],
                template_content_hash, stamp,
            ),
        )
        template_after = {
            "id": template_id, "key": "fixture_template", "origin": "manual",
            "status": "active", "generation": 1,
            "current_version_id": template_version_id,
            "version": {
                **template_content, "id": template_version_id,
                "version_number": 1, "content_hash": template_content_hash,
            },
        }
        template_result = {
            "generation": 1, "idempotent": False, "ok": True,
            "status": "active", "template_id": template_id,
            "version_id": template_version_id, "version_number": 1,
        }
        db.execute(
            "INSERT INTO task_template_events "
            "(template_id,template_version_id,event_type,generation,actor_id,"
            "operation_id,request_hash,note,before_json,after_json,result_json,created_at) "
            "VALUES (?,?,'created',1,101,'task-template-create-fixture',?,'',?,?,?,?)",
            (
                template_id, template_version_id, template_content_hash,
                json.dumps({}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(template_after, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(template_result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO tasks "
            "(id,type,title,details,address,city,reward,status,created_by,created_at,"
            "repeatable,photo_file,photo_media_id,operation_id,request_hash,"
            "submission_attempt,evidence_policy,max_participants,budget_cap,"
            "template_id,template_version_id,version) "
            "VALUES (1001,'fix_zone','Тестовое задание','Без реальных данных',"
            "'Тестовый адрес','Краснодар',100,'closed',101,?,0,'media-fixture.jpg',"
            "'media-fixture','task-create-fixture','task-request-fixture',1,"
            "'photo_required',1,100,?,?,1)",
            (stamp, template_id, template_version_id),
        )
        db.execute(
            "INSERT INTO task_assignments "
            "(id,task_id,user_id,status,claimed_at,done_at,proof_note,"
            "completion_operation_id,completion_request_hash,submission_attempt,"
            "reward_snapshot,terminal_at,terminal_by,terminal_reason,"
            "decision_operation_id,decision_request_hash,version) "
            "VALUES (2001,1001,102,'done',?,?,'Готово','complete-fixture',"
            "'complete-request',1,100,?,101,'approved','review-fixture',"
            "'review-request',1)", (stamp, stamp, stamp),
        )
        db.execute(
            "INSERT INTO task_evidence "
            "(id,assignment_id,task_id,user_id,kind,photo_file,media_id,sha256,"
            "submission_operation_id,attempt,is_current,created_at) "
            "VALUES (3001,2001,1001,102,'after','media-fixture.jpg','media-fixture',?,"
            "'complete-fixture',1,1,?)", ("a" * 64, stamp),
        )
        db.execute(
            "INSERT INTO withdrawal_requests "
            "(id,user_id,amount,status,created_at,operation_id,request_hash,"
            "account_type,account_ciphertext,account_masked,account_fingerprint,key_version) "
            "VALUES (4001,102,50,'pending',?,'withdraw-fixture','withdraw-request',"
            "'account_id','opaque-fernet-value','10••02',?,1)",
            (stamp, "b" * 64),
        )
        db.execute(
            "INSERT INTO withdrawal_events "
            "(id,withdrawal_id,event_type,from_status,to_status,actor_id,operation_id,"
            "created_at,metadata_json) VALUES (4101,4001,'created',NULL,'pending',102,"
            "'withdraw-fixture',?,'{}')", (stamp,),
        )
        db.executemany(
            "INSERT INTO bonus_ledger "
            "(id,user_id,amount,reason,task_id,assignment_id,withdrawal_id,created_by,"
            "created_at,operation_id,balance_after) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (5001,102,100,"Награда",1001,2001,None,101,stamp,"reward-fixture",100),
                (5002,102,-50,"Перевод",None,None,4001,102,stamp,"debit-fixture",50),
            ],
        )
        db.execute(
            "INSERT INTO referral_rewards (referee_id,referrer_id,amount,created_at) "
            "VALUES (102,101,0,?)", (stamp,),
        )
        db.execute(
            "INSERT INTO referral_tokens (token,referrer_id,created_at,expires_at) "
            "VALUES ('fixture-referral-token',101,?,?)", (stamp, stamp),
        )
        db.execute(
            "INSERT INTO referral_milestone_rewards (user_id,threshold,amount,created_at) "
            "VALUES (101,1,0,?)", (stamp,),
        )
        db.execute(
            "INSERT INTO task_review_commands "
            "(operation_id,assignment_id,request_hash,result_status,created_at) "
            "VALUES ('review-fixture',2001,'review-request','done',?)", (stamp,),
        )
        manual_ledger_id = db.execute(
            "INSERT INTO bonus_ledger "
            "(user_id,amount,reason,created_by,created_at,operation_id,balance_after) "
            "VALUES (102,10,'Ручная благодарность',101,?,'manual-grant-fixture',60)",
            (stamp,),
        ).lastrowid
        db.execute(
            "INSERT INTO manual_grant_commands "
            "(operation_id,request_hash,user_id,amount,reason,maker_id,created_at,"
            "ledger_id,result_balance) VALUES ('manual-grant-fixture',?,102,10,"
            "'Тестовая благодарность',101,?,?,60)",
            ("1" * 64, stamp, manual_ledger_id),
        )
        db.execute(
            "INSERT INTO manual_grant_reversals "
            "(id,grant_operation_id,original_ledger_id,user_id,amount,reason,status,"
            "requested_by,requested_at,request_operation_id,request_hash,decided_by,"
            "decided_at,decision_note,decision_operation_id,decision_hash) "
            "VALUES (5601,'manual-grant-fixture',?,102,10,'Проверка fixture','rejected',"
            "101,?,'manual-reversal-request-fixture',?,103,?,'Начисление верно',"
            "'manual-reversal-decision-fixture',?)",
            (
                manual_ledger_id, stamp, "2" * 64, stamp, "3" * 64,
            ),
        )
        db.execute(
            "INSERT INTO task_disputes "
            "(id,assignment_id,task_id,user_id,reward,reason,status,opened_by,opened_at,"
            "open_operation_id,open_request_hash,decided_by,decided_at,decision_note,"
            "decision_operation_id,decision_request_hash) "
            "VALUES (5501,2001,1001,102,100,'Проверка fixture','rejected',101,?,"
            "'dispute-open-fixture','dispute-open-request',103,?,'Решение верно',"
            "'dispute-decision-fixture','dispute-decision-request')",
            (stamp, stamp),
        )
        db.execute(
            "INSERT INTO task_completion_commands "
            "(operation_id,assignment_id,request_hash,result_status,created_at) "
            "VALUES ('complete-fixture',2001,'complete-request','review',?)", (stamp,),
        )
        db.execute(
            "INSERT INTO task_outbox "
            "(id,event_key,event_type,recipient_id,payload_json,status,attempts,"
            "available_at,created_at,sent_at,telegram_message_id,telegram_thread_id) "
            "VALUES (6001,'outbox-fixture','direct',102,?,'sent',1,?,?,?,7001,1)",
            (json.dumps({"text": "ok"}), stamp, stamp, stamp),
        )
        db.execute(
            "INSERT INTO telegram_update_inbox "
            "(update_id,payload_json,payload_sha256,status,attempts,available_at,received_at) "
            "VALUES (99001,?,?,'pending',0,?,?)",
            (
                application._encrypt_telegram_payload(update_payload),
                application._telegram_payload_fingerprint(update_payload), stamp, stamp,
            ),
        )
        db.execute(
            "INSERT INTO telegram_update_effects (update_id,effect_key,created_at) "
            "VALUES (99001,'fixture-effect',?)", (stamp,),
        )
        db.execute(
            "INSERT INTO telegram_update_redrive_commands "
            "(operation_id,request_hash,update_id,admin_id,reason,result_status,created_at) "
            "VALUES ('redrive-fixture','redrive-request',99001,101,'Тест','pending',?)",
            (stamp,),
        )
        db.execute(
            "INSERT INTO chat_activity "
            "(user_id,last_msg_at,day,msg_xp_today,thanks_xp_today,messages_total,thanks_total) "
            "VALUES (102,?,'2026-07-27',1,1,1,1)", (stamp,),
        )
        db.execute(
            "INSERT INTO analytics_subjects (subject_id,user_id,created_at) "
            "VALUES ('subject-fixture',102,?)", (stamp,),
        )
        db.execute(
            "INSERT INTO product_events "
            "(id,event_id,occurred_at,event_name,source,subject_id,task_id,assignment_id,"
            "outcome,properties_json,schema_version,expires_at) VALUES (7001,'event-fixture',"
            "?,'task_created','backend','subject-fixture',1001,2001,'ok','{}',1,?)",
            (stamp, stamp),
        )
        db.execute(
            "INSERT INTO published_posts "
            "(kind,chat_id,topic,message_ids,published_at,published_by,operation_id) "
            "VALUES ('news',-100123,1,'[101]',?,101,'publication-fixture')", (stamp,),
        )
        db.execute(
            "INSERT INTO publication_jobs "
            "(kind,operation_id,status,requested_by,created_at,completed_at) "
            "VALUES ('news','publication-fixture','done',101,?,?)", (stamp, stamp),
        )
        db.execute(
            "INSERT INTO publication_delivery_parts "
            "(operation_id,part_index,message_id,created_at) "
            "VALUES ('publication-fixture',0,101,?)", (stamp,),
        )
        db.execute(
            "INSERT INTO publication_cleanup_messages "
            "(operation_id,chat_id,message_id,final_job_status,status,attempts,deleted_at) "
            "VALUES ('publication-fixture','-100123',99,'done','deleted',1,?)", (stamp,),
        )
        db.execute(
            "INSERT INTO thanks_pairs (from_id,to_id,last_at) VALUES (101,102,?)", (stamp,),
        )
        award_id = db.execute("SELECT id FROM awards ORDER BY id LIMIT 1").fetchone()[0]
        db.execute(
            "INSERT INTO member_awards "
            "(id,user_id,award_id,slot,bonus,note,granted_by,granted_at,operation_id,"
            "balance_after) VALUES (8001,102,?,'fixture',0,'Тест',101,?,"
            "'award-fixture',60)", (award_id, stamp),
        )
        award_title = db.execute(
            "SELECT title FROM awards WHERE id=?", (award_id,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO award_reversals "
            "(id,member_award_id,original_ledger_id,user_id,award_id,award_title,"
            "amount,original_granted_by,original_grant_operation_id,origin,status,"
            "reason,requested_by,requested_at,request_operation_id,request_hash,"
            "decided_by,decided_at,decision_note,decision_operation_id,decision_hash,"
            "version) VALUES (8101,8001,NULL,102,?,?,0,101,'award-fixture',"
            "'maker_checker','rejected','Проверка fixture',101,?,"
            "'award-reversal-request-fixture',?,103,?,'Награда выдана верно',"
            "'award-reversal-decision-fixture',?,2)",
            (award_id, award_title, stamp, "6" * 64, stamp, "7" * 64),
        )
        db.executemany(
            "INSERT INTO award_reversal_events "
            "(id,reversal_id,event_type,from_status,to_status,actor_id,operation_id,"
            "created_at,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    8201, 8101, "requested", None, "pending", 101,
                    "award-reversal-request-fixture", stamp, "{}",
                ),
                (
                    8202, 8101, "rejected", "pending", "rejected", 103,
                    "award-reversal-decision-fixture", stamp, "{}",
                ),
            ],
        )
        db.executemany(
            "INSERT INTO operation_registry "
            "(operation_id,command_type,request_hash,actor_id,created_at) "
            "VALUES (?,?,?,?,?)",
            [
                ("award-fixture", "award_grant", "f" * 64, 101, stamp),
                ("manual-grant-fixture", "manual_grant", "1" * 64, 101, stamp),
                (
                    "manual-reversal-request-fixture",
                    "manual_grant_reversal_request", "2" * 64, 101, stamp,
                ),
                (
                    "manual-reversal-decision-fixture",
                    "manual_grant_reversal_decision", "3" * 64, 103, stamp,
                ),
                (
                    "access-request-fixture",
                    "staff_access_request", "4" * 64, 101, stamp,
                ),
                (
                    "task-template-create-fixture", "task_template_create",
                    template_content_hash, 101, stamp,
                ),
                (
                    "award-reversal-request-fixture", "award_reversal_request",
                    "6" * 64, 101, stamp,
                ),
                (
                    "award-reversal-decision-fixture", "award_reversal_decision",
                    "7" * 64, 103, stamp,
                ),
            ],
        )
        db.commit()
    print(application.DB_PATH)


if __name__ == "__main__":
    main()
