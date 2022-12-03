# Generate validator keys for Ethereum with trusted code in AWS Lambda and AWS Signer

## Personas

* **Developer** - update and build code and package unsigned code to S3
* **Release Manager** - reviews, signs and deploys code

## Code Deployment

* Install [Amazon SAM CLI](https://docs.amazonaws.cn/en_us/serverless-application-model/latest/developerguide/install-sam-cli.html)
* Install [yq](https://github.com/mikefarah/yq/releases/tag/v4.30.4)
* Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) for cross-compile support

### Create S3 bucket

Release Manager creates S3 bucket to host the code

```bash
# !!!Make sure you change the region name to the one you use!!!
export AWS_DEFAULT_REGION=ap-southeast-2
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
BUCKET_NAME=secure-keygen-sam-$ACCOUNT_ID

aws s3 mb s3://$BUCKET_NAME

aws s3api put-bucket-versioning --bucket $BUCKET_NAME --versioning-configuration MFADelete=Disabled,Status=Enabled
```

### Create Signing Profile

Release Manager creates signing profile

```bash
aws signer put-signing-profile --platform-id "AWSLambda-SHA384-ECDSA" --profile-name Test1
```

### Create Developer user

Create developer user, record the credentials and create a new profile based on the credentials

```bash
cat <<EOT > s3access.json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject"
      ],
      "Resource": [
        "arn:aws:s3:::$BUCKET_NAME/*"
      ]
    }
  ]
}
EOT


aws iam create-user --user-name secure-keygen-dev
aws iam put-user-policy --user-name secure-keygen-dev --policy-name S3Access --policy-document file://s3access.json
aws iam create-access-key --user-name secure-keygen-dev
```

### Build and package

Open a new terminal and use the developer profile created previously.

```bash
# Developer build the packages
sam build --use-container

# Upload the package to S3
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
BUCKET_NAME=secure-keygen-sam-$ACCOUNT_ID
export AWS_DEFAULT_REGION=ap-southeast-2 # !!!Make sure you change the region name to the one you use!!!
sam package --s3-bucket $BUCKET_NAME --output-template-file .aws-sam/build/template.yaml --region $AWS_DEFAULT_REGION
```

Developer should have deny access to perform code signing. Developer sends over `template.yaml` to Release Manager

### Deploy

Release manager downloads and reviews the unsigned code. Developer also passes the generated CloudFormation template file i.e. `./.aws-sam/build/template.yaml` file to Release Manager.

```bash
CODE_S3_LOCATION=$(cat .aws-sam/build/template.yaml | yq '.Resources.ValidatorKeyGenFunction.Properties.CodeUri')

mkdir -p review
aws s3 cp $CODE_S3_LOCATION ./review/source.zip
```

To review the code, unzip the file:

```bash
cd ./review
unzip source.zip
```

After review, release manager signs the code and updates the stack with the signed code

```bash
SIGNING_PROFILE_ARN=$(aws signer get-signing-profile --profile-name Test1 --query profileVersionArn --output text)

sam deploy \
 --region $AWS_DEFAULT_REGION \
 --stack-name secure-keygen \
 --template-file ./.aws-sam/build/template.yaml \
 --parameter-overrides SigningProfileVersionArnParameter=$SIGNING_PROFILE_ARN \
 --signing-profiles ValidatorKeyGenFunction=Test1 \
 --confirm-changeset \
 --capabilities CAPABILITY_IAM \
 --no-disable-rollback
```

## Generate keys

We need to invoke the Lambda function to generate the keys. Observe the [payload](events/lambdaPayload.json) structure to understand the parameters expected by the Lambda function

```bash
FUNCTION_ARN=$(aws cloudformation describe-stacks --stack-name secure-keygen --query "Stacks[0].Outputs[?OutputKey=='ValidatorKeyGenFunction'].OutputValue" --output text)

aws lambda invoke --function-name $FUNCTION_ARN --payload fileb://events/lambdaPayload.json output.json
```

Upon completion of the Lambda function execution, observe that there are new rows inserted into the DynamoDB table. You can find the DynamoDB table ARN by issuing the following command:

```bash
aws cloudformation describe-stacks --stack-name secure-keygen --query "Stacks[0].Outputs[?OutputKey=='ValidatorKeysTable'].OutputValue" --output text
```

Each DynamoDB table entry has the following field:

* **web3signer_uuid** - Used to assign the keys to Web3Signer instances. Each instance is assigned a UUID, and will query the table using this field. To assign the keys to a specific instance, modify the data in this field accordingly
* **pubkey** - The public key
* **active** - Applications such as Web3Signer can optionally use this field to determine whether to load the keys
* **chain** - Ethereum chain
* **datetime** - The date and time in which the key is generated
* **deposit_json_b64** - The deposit JSON data in base64 encoding. Decode this data and upload it to [Ethereum Staking Launchpad](https://launchpad.ethereum.org)
* **encrypted_key_password_mnemonic_b64** - KMS-encrypted JSON-formatted data structure consisting of:
  * `keystore_b64` - Encrypted BLS12-381 keystore in base64 encoding
  * `password_b64` - randomly generated password to decrypt the keystore in base64 encoding
  * `mnemonic_b64` mnemonic from which the private key is derived from in base64 encoding

## Cleanup

To delete the sample application that you created, use the AWS CLI. Assuming you used your project name for the stack name, you can run the following:

```bash
aws cloudformation delete-stack --stack-name secure-keygen
```

Delete the developer user from IAM

```bash
aws iam delete-user --user-name secure-keygen-dev
```

Delete the Signer profile

```bash
PROFILE_VERSION=$(aws signer get-signing-profile --profile-name Test1 --query profileVersion --output text)
aws signer revoke-signing-profile --profile-name Test1 \
--profile-version $PROFILE_VERSION \
--reason "Unused" \
--effective-time $(date +%s)
```

Delete the deployment S3 bucket and its contents

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
BUCKET_NAME=secure-keygen-sam-$ACCOUNT_ID

aws s3api delete-objects \
      --bucket $BUCKET_NAME \
      --delete "$(aws s3api list-object-versions \
      --bucket $BUCKET_NAME | \
      jq '{Objects: [.Versions[] | {Key:.Key, VersionId : .VersionId}], Quiet: false}')"

aws s3api delete-bucket --bucket $BUCKET_NAME
```

## Development

* Install [Python 3.9](https://www.python.org/downloads/release/python-390/)
* Install [Poetry](https://python-poetry.org/docs/)

```bash
# Install dependencies
poetry install

# Activate virtual environment
source $(poetry env info --path)/bin/activate
```

If you modify the dependencies, export the `requirements.txt` file

```bash
poetry export --without dev --without-hashes > secure_keygen/requirements.txt
```
