# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import secrets
import base64
import logging
import os
import boto3
from botocore.exceptions import ClientError
from datetime import datetime

from eth_utils import is_hex_address, to_normalized_address
from staking_deposit.settings import get_chain_setting, ALL_CHAINS
from staking_deposit.credentials import Credential
from staking_deposit.utils.validation import (
    validate_deposit,
)
from staking_deposit.key_handling.keystore import (
    Keystore,
)
from staking_deposit.exceptions import ValidationError
from staking_deposit.utils.constants import (
    MNEMONIC_LANG_OPTIONS,
    MAX_DEPOSIT_AMOUNT,
)
from staking_deposit.key_handling.key_derivation.mnemonic import (
    get_mnemonic,
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG")
LOG_FORMAT = "%(levelname)s:%(lineno)s:%(message)s"
handler = logging.StreamHandler()

logger = logging.getLogger("validator_keygen")
logger.setLevel(LOG_LEVEL)
logger.addHandler(handler)
logger.propagate = False

kms_key_arn = os.getenv("KMS_KEY_ARN")
table_name = os.getenv("DDB_TABLE_NAME")

client_kms = boto3.client("kms")
dynamodb = boto3.resource("dynamodb")

words_list_path = "word_lists"


def verify_keystore(credential: Credential, keystore: Keystore, password: str) -> bool:
    """Verify keystore"""

    secret_bytes = keystore.decrypt(password)
    return credential.signing_sk == int.from_bytes(secret_bytes, "big")


def lambda_handler(event, context) -> list:
    """
    Example request
    {
     "num_validators": 2,
     "mnemonic_language": "english",
     "chain": "goerli",
     "eth1_withdrawal_address": "0x6F4b46423fc6181a0cF34e6716c220BD4d6C2471"
    }
    """

    logger.debug("incoming event: {}".format(event))

    if kms_key_arn is None:
        raise ValueError("Specify KMS_KEY_ARN environment variable")

    if table_name is None:
        raise ValueError("Specify DDB_TABLE_NAME environment variable")

    num_validators = event.get("num_validators", 1)

    if num_validators not in range(1, 10):
        message = "Number of validators should be between 1 and 10"
        logger.fatal(message)
        raise ValueError(message)

    mnemonic_language = event.get("mnemonic_language", "english")
    mnemonic_language = mnemonic_language.lower()

    if mnemonic_language not in MNEMONIC_LANG_OPTIONS:
        message = "Mnemonic language is invalid"
        logger.fatal(message)
        raise ValueError(message)

    chain = event.get("chain", "goerli")
    chain = chain.lower()

    if chain not in ALL_CHAINS:
        message = "Chain is invalid"
        logger.fatal(message)
        raise ValueError(message)

    eth1_withdrawal_address = event.get("eth1_withdrawal_address", None)

    if eth1_withdrawal_address is not None:
        if not is_hex_address(eth1_withdrawal_address):
            message = "Eth1 address is not in hexadecimal encoded form."
            logger.fatal(message)
            raise ValueError(message)

        eth1_withdrawal_address = to_normalized_address(eth1_withdrawal_address)

    logger.info(
        "Start:\nnum_validators = %d\nchain = %s\nmnemonic_language = %s\nwithdrawal_address = %s",
        num_validators,
        chain,
        mnemonic_language,
        eth1_withdrawal_address,
    )

    mnemonic = get_mnemonic(language=mnemonic_language, words_path=words_list_path)

    logger.info("Mnemonic generated!")

    chain_setting = get_chain_setting(chain)
    validator_start_index = 0

    amounts = [MAX_DEPOSIT_AMOUNT] * num_validators

    if len(amounts) != num_validators:
        raise ValueError(
            f"The number of keys ({num_validators}) doesn't equal to the corresponding deposit amounts ({len(amounts)})."
        )

    key_indices = range(validator_start_index, validator_start_index + num_validators)

    # No mnemonic password needed
    mnemonic_password = ""  # nosec

    credentials_list = [
        Credential(
            mnemonic=mnemonic,
            mnemonic_password=mnemonic_password,
            index=index,
            amount=amounts[index - validator_start_index],
            chain_setting=chain_setting,
            hex_eth1_withdrawal_address=eth1_withdrawal_address,
        )
        for index in key_indices
    ]

    validator_key_records = []

    for index, credential in enumerate(credentials_list):
        password = secrets.token_urlsafe(14)
        keystore = credential.signing_keystore(password)
        encrypted_key = keystore.as_json()
        encrypted_key_obj = json.loads(encrypted_key)
        pub_key = encrypted_key_obj["pubkey"]
        logger.info(
            "%d / %d - Encrypted validator key generated - pubkey: %s", index + 1, len(credentials_list), pub_key
        )

        deposit_data_dict = credential.deposit_datum_dict
        deposit_data = json.dumps(deposit_data_dict, default=lambda x: x.hex())
        logger.info("%d / %d - Deposit data generated - pubkey: %s", index + 1, len(credentials_list), pub_key)

        if not verify_keystore(credential=credential, keystore=keystore, password=password):
            message = "Failed to verify the keystores"
            logger.fatal(message)
            raise ValidationError(message)

        if not validate_deposit(json.loads(deposit_data), credential):
            message = "Failed to verify the deposit"
            logger.fatal(message)
            raise ValidationError(message)

        to_encrypt_by_kms = {
            "keystore_b64": base64.b64encode(encrypted_key.encode("ascii")).decode("ascii"),
            "password_b64": base64.b64encode(password.encode("ascii")).decode("ascii"),
            "mnemonic_b64": base64.b64encode(mnemonic.encode("ascii")).decode("ascii"),
        }

        logger.info("%d / %d - Encrypting key, password and mnemonic using KMS", index + 1, len(credentials_list))

        try:
            response = client_kms.encrypt(KeyId=kms_key_arn, Plaintext=json.dumps(to_encrypt_by_kms).encode())

        except Exception as e:
            raise Exception("Exception happened sending encryption request to KMS: {}".format(e))

        encrypted_key_password_mnemonic_b64 = base64.standard_b64encode(response["CiphertextBlob"]).decode()

        deposit_data_list = f"[{deposit_data}]"

        record = {
            "web3signer_uuid": "none",
            "chain": chain,
            "pubkey": pub_key,
            "encrypted_key_password_mnemonic_b64": encrypted_key_password_mnemonic_b64,
            "deposit_json_b64": base64.b64encode(deposit_data_list.encode("ascii")).decode("ascii"),
            "datetime": datetime.now().isoformat(),
            "active": True,
        }

        logger.debug("%d / %d - Record - {}".format(record), index + 1, len(credentials_list))

        validator_key_records.append(record)

    logger.info("Writing validator keys record to DynamoDB")

    try:
        table = dynamodb.Table(table_name)
        with table.batch_writer() as writer:
            for record in validator_key_records:
                writer.put_item(Item=record)
    except ClientError as err:
        logger.error(
            "Couldn't load data into table %s. Here's why: %s: %s",
            table.name,
            err.response["Error"]["Code"],
            err.response["Error"]["Message"],
        )
        raise

    logger.info("Successfully written validator keys record to DynamoDB")

    pubkey_list = list(map(lambda record: record["pubkey"], validator_key_records))

    return pubkey_list
