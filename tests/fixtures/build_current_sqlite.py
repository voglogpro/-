"""Build a synthetic, PII-free current-schema SQLite source for migration CI."""

from __future__ import annotations

import argparse
import asyncio
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
            "'helper','approved',50,1,101,?,?,?,?,0,1)",
            (stamp, stamp, 101, stamp),
        )
        db.execute(
            "INSERT INTO media_objects "
            "(id,backend,object_key,purpose,state,content_type,size_bytes,sha256,"
            "upload_operation_id,request_hash,created_at,ready_at,reconcile_attempts) "
            "VALUES ('media-fixture','local','media-fixture.jpg','task_proof','ready',"
            "'image/jpeg',4,?,'media-upload-fixture','media-request-fixture',?,?,0)",
            ("a" * 64, stamp, stamp),
        )
        db.execute(
            "INSERT INTO tasks "
            "(id,type,title,details,address,city,reward,status,created_by,created_at,"
            "repeatable,photo_file,photo_media_id,operation_id,request_hash,"
            "submission_attempt,evidence_policy,max_participants,budget_cap,version) "
            "VALUES (1001,'fix_zone','Тестовое задание','Без реальных данных',"
            "'Тестовый адрес','Краснодар',100,'closed',101,?,0,'media-fixture.jpg',"
            "'media-fixture','task-create-fixture','task-request-fixture',1,"
            "'photo_required',1,100,1)", (stamp,),
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
            "balance_after) VALUES (8001,101,?,'fixture',0,'Тест',101,?,"
            "'award-fixture',0)", (award_id, stamp),
        )
        db.commit()
    print(application.DB_PATH)


if __name__ == "__main__":
    main()
