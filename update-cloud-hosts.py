#!/usr/local/bin/python
# coding: UTF-8

import getpass
import boto3
import configparser
import json
import os
import yaml
import digitalocean
import shutil
import concurrent.futures
import base64
from inputimeout import inputimeout, TimeoutOccurred
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from pathlib import Path
import platform
import argparse


# Outputs to stdout the list of instances containing the following fields:
# name          => Instance name formed by the instance class/group and the domain prefix with appropriate cloud provider (aws., DO.,)
# group         => Group associated with the instance (webapp, vpn, etc.)
# index         => Index of this instance in the group

def decrypt(ciphertext, keyfile):
    if not os.path.isfile(os.path.expanduser(keyfile)):
        return [False, f"Decryption key not found at {keyfile}."]
    input = open(os.path.expanduser(keyfile))
    key = RSA.importKey(input.read())
    input.close()
    cipher = PKCS1_v1_5.new(key)
    plaintext = cipher.decrypt(ciphertext, None).decode('utf-8')
    return [True, plaintext]

def settingResolver(setting,instance,vpc_data_all,caller_type='AWS', setting_value = False):
    if caller_type == 'AWS':
        setting_value = get_tag_value(instance.get('Tags', ''), setting, False, setting_value)
    if caller_type == 'DO':
        setting_value = get_DO_tag_value(instance.tags, setting, setting_value)
    if setting_value == False:
        if caller_type == 'AWS' and instance['State']['Name'] != "terminated":
            setting_value = vpc_data(instance['VpcId'], setting, vpc_data_all)
        if caller_type == 'DO':
            pass
        if setting_value == False:
            if 'iTerm_' in setting:
                setting = setting.rpartition('iTerm_')[2] # Strip iTerm prefix because settings are now read from conf files
            if 'Cloud_Profiler_' in setting:
                setting = setting.rpartition('Cloud_Profiler_')[2] # Strip iTerm prefix because settings are now read from conf files
            setting_value = profile.get(setting, False)
            if setting_value == False:
                setting_value = script_config[caller_type].get(setting, False)
                if setting_value == False:
                    setting_value = script_config["Local"].get(setting, False)
    return setting_value

def get_DO_tag_value(tags,q_tag, q_tag_value):
            tag_key=''
            tag_value=''
            for tag in tags:
                if ':' in tag and ('iTerm' in tag or 'Cloud_Profiler' in tags):
                    tag_key,tag_value = tag.split(':')
                    if tag_key == q_tag:
                        q_tag_value = tag_value.replace('_', ' ')
                        q_tag_value = tag_value.replace('-', '.')
                        break
            return q_tag_value
            
def get_tag_value(tags, q_tag, sg=False, q_tag_value = False):
    for tag in tags:
        if 'iTerm_' in tag.get('Key', ''):
            tag['Key'] = tag['Key'].rpartition('iTerm_')[2]
        if 'Cloud_Profiler_' in tag.get('Key', ''):
            tag['Key'] = tag['Key'].rpartition('Cloud_Profiler_')[2]
        if q_tag == 'flat' and not sg:
            if not q_tag_value:
                q_tag_value = ''
            q_tag_value += tag['Key'] + ': ' + tag['Value'] + ","
        elif q_tag == 'flat' and sg == "sg":
            if not q_tag_value:
                q_tag_value = ''
            q_tag_value += tag['GroupName'] + ': ' + tag['GroupId'] + ","
        else:
            if tag['Key'] == q_tag:
                q_tag_value = tag['Value']
                if tag['Value'] == 'True' or tag['Value'] == "yes" or tag['Value'] == "y":
                    q_tag_value = True
                if tag['Value'] == 'False' or tag['Value'] == 'no' or tag['Value'] == "n":
                    q_tag_value = False
                break
    return q_tag_value


def vpc_data(vpcid, q_tag, response_vpc):
    q_tag_value = False
    for vpc in response_vpc['Vpcs']:
        if vpcid == vpc['VpcId']:
            if vpc.get('Tags', False):
                if q_tag == "flat":
                    for tag in vpc.get('Tags'):
                        if "iTerm" in tag['Key'] or 'Cloud_Profiler' in tag['Key']:
                            if not q_tag_value:
                                q_tag_value = ''
                            q_tag_value += "VPC." + tag['Key'] + ': ' + tag['Value'] + ","
                else:
                    q_tag_value = get_tag_value(vpc.get('Tags'), q_tag)
    return q_tag_value

def getDOInstances(profile):
    instance_source = "DO." + profile['name']
    groups = {}
    instances = {}
    global instance_counter
        
    instance_counter[instance_source] = 0
    manager = digitalocean.Manager(token=profile['token'])
    my_droplets = manager.get_all_droplets()

    for drop in my_droplets:
        if (script_config['DO'].get('skip_stopped', True) == True \
            and script_config['Local'].get('skip_stopped', True) == True \
            and profile.get('skip_stopped', True) == True) \
            and drop.status != 'active':
            continue
        
        password = [False, ""]
        iterm_tags = []
        instance_use_ip_public = settingResolver('use_ip_public',drop, {}, "DO", False)
        instance_use_bastion = settingResolver('use_bastion',drop, {}, "DO", False)
        or_host_name=settingResolver('host_name',drop,{},"DO", False)
        bastion = settingResolver('bastion',drop,{},"DO", False)
        con_username = settingResolver('con_username',drop,{},"DO", False)
        bastion_con_username = settingResolver('bastion_con_username',drop,{},"DO", False)
        con_port = settingResolver('con_port',drop,{},"DO", 22)
        bastion_con_port = settingResolver('bastion_con_port',drop,{},"DO", 22)
        ssh_key = settingResolver('ssh_key',drop,{}, "DO", False)
        use_shared_key = settingResolver('use_shared_key',drop,{},"DO", False)
        dynamic_profile_parent_name = settingResolver('dynamic_profile_parent_name',drop,{},"DO")
        public_ip = drop.ip_address

        if or_host_name:
            drop_name = or_host_name
        else:
            drop_name = drop.name

        if instance_use_ip_public:
            ip = drop.ip_address
        else:
            ip = drop.private_ip_address
            
        if drop.name in drop.tags:
            groups[drop.name] = groups[drop.name] + 1
        else:
            groups[drop.name] = 1

        if drop.tags:
            for tag in drop.tags:
                if tag:
                    iterm_tags.append(tag)
        
        iterm_tags += ip,drop.name,drop.size['slug']
        instances[ip] = {'name':instance_source + '.' + drop_name,
                        'group': drop_name,
                        'index':groups[drop.name],
                        'dynamic_profile_parent_name': dynamic_profile_parent_name,
                        'iterm_tags': iterm_tags, 'InstanceType': drop.size['slug'],
                        'con_username': con_username,
                        'bastion_con_username': bastion_con_username,
                        'con_port': con_port,
                        'bastion_con_port': bastion_con_port,
                        'id': drop.id,
                        'ssh_key': ssh_key,
                        'use_shared_key': use_shared_key,
                        'instance_use_bastion': instance_use_bastion,
                        'bastion': bastion,
                        'instance_use_ip_public': instance_use_ip_public,
                        'ip_public': public_ip,
                        'password': password,
                        'region': drop.region['name']}
        print(f'instance_source: {ip}\t\t{instance_source}. {drop_name}\t\tassociated bastion: "{str(bastion)}"')
    
    cloud_instances_obj_list.append({"instance_source": instance_source, "groups": groups, "instances":instances})

def fetchEC2Instance(instance, client, groups, instances, instance_source, reservation, vpc_data_all):
    instance_vpc_flat_tags = ''
    instance_flat_tags = ''
    iterm_tags = []
    password = [False, ""]

    instance_use_bastion = settingResolver('use_bastion', instance, vpc_data_all,'AWS', False)
    instance_use_ip_public = settingResolver('use_ip_public', instance, vpc_data_all,'AWS', False)
    ssh_key = settingResolver('ssh_key', instance, vpc_data_all,'AWS', instance.get('KeyName',False))
    use_shared_key = settingResolver('use_shared_key', instance, vpc_data_all,'AWS', False)
    con_username = settingResolver('con_username', instance, vpc_data_all,'AWS', False)
    bastion_con_username = settingResolver('bastion_con_username', instance, vpc_data_all,'AWS', False)
    con_port = settingResolver('con_port', instance, vpc_data_all,'AWS', 22)
    bastion_con_port = settingResolver('bastion_con_port', instance, vpc_data_all,'AWS', 22)
    bastion = settingResolver('bastion', instance, vpc_data_all,'AWS', False)
    dynamic_profile_parent_name = settingResolver('dynamic_profile_parent_name', instance, vpc_data_all,'AWS', False)
    instance_vpc_flat_tags = vpc_data(instance.get('VpcId', ''), "flat", vpc_data_all)
    instance_flat_sgs = ''
    for interface in instance.get('NetworkInterfaces',[]):
        instance_flat_sgs += (get_tag_value(interface['Groups'],'flat',"sg"))
    
    if not ssh_key:
        ssh_key = instance.get('KeyName', '')

    if 'Tags' in instance:
        name = get_tag_value(instance['Tags'], "Name" ,False, instance['InstanceId'])
        instance_flat_tags = get_tag_value(instance['Tags'], 'flat')
    else:
        name = instance['InstanceId']

    if instance_use_ip_public == True and 'PublicIpAddress' in instance:
        ip = instance['PublicIpAddress']
    else:
        try:
            ip = instance['NetworkInterfaces'][0]['PrivateIpAddress']
        except IndexError:
            ip = r"No IP found at scan time ¯\_(ツ)_/¯, probably a terminated instance. (Sorry)#"

    if name in groups:
        groups[name] = groups[name] + 1
    else:
        groups[name] = 1

    if 'PublicIpAddress' in instance:
        public_ip = instance['PublicIpAddress']
        iterm_tags.append(instance['PublicIpAddress'])
    else:
        public_ip = ''
    
    if instance_flat_tags:
        iterm_tags.append(instance_flat_tags)
    if instance_vpc_flat_tags:
        iterm_tags.append(instance_vpc_flat_tags)
    if instance_flat_sgs:
        iterm_tags.append(instance_flat_sgs)

    iterm_tags.append(instance.get('VpcId',''))
    iterm_tags.append(instance['InstanceId'])
    iterm_tags.append(instance['Placement']['AvailabilityZone'])
    iterm_tags.append(instance['InstanceType'])
    if instance['PublicDnsName']:
        iterm_tags.append(instance['PublicDnsName'])
    
    iterm_tags_fin = []
    for tag in iterm_tags:
        if ',' in tag:
            for shard in tag.split(','):
                if shard.strip():
                    iterm_tags_fin.append(shard)
        else:
            iterm_tags_fin.append(tag)
    
    
    if instance.get('Platform', '') == 'windows':
        response =  client.get_password_data(
                    InstanceId=instance['InstanceId'],
                    )
        data = base64.b64decode(response['PasswordData'])
        password = decrypt(data, os.path.join(script_config["Local"].get('ssh_keys_path', '.'),ssh_key))
    
    instances[ip] = {'name': instance_source + '.' + name, 'index': groups[name], 'group': name,
                     'bastion': bastion, 'vpc': instance.get('VpcId', ""),
                     'instance_use_ip_public': instance_use_ip_public,
                     'instance_use_bastion': instance_use_bastion,
                     'ip_public': public_ip,
                     'dynamic_profile_parent_name': dynamic_profile_parent_name, 'iterm_tags': iterm_tags_fin,
                     'InstanceType': instance['InstanceType'],
                     'con_username': con_username,
                     'bastion_con_username': bastion_con_username,
                     'con_port': con_port,
                     'bastion_con_port': bastion_con_port,
                     'id': instance['InstanceId'],
                     'ssh_key': ssh_key,
                     'use_shared_key': use_shared_key,
                     'platform': instance.get('Platform', ''),'password': password,
                     'region': instance['Placement']['AvailabilityZone'][:-1]}
    return (ip + "\t" + instance['Placement']['AvailabilityZone'] + "\t" + instance_source + "." + name + "\t\t associated bastion: \"" + str(bastion) + "\"")


def fetchEC2Region(region, profile_name, instances, groups, instance_source, credentials = False):
    if region in script_config['AWS']['exclude_regions']:
        print(f'{instance_source}: region "{region}", is in excluded list')
        return

    if credentials:
        client = boto3.client('ec2',
                            aws_access_key_id=credentials['AccessKeyId'],
                            aws_secret_access_key=credentials['SecretAccessKey'],
                            aws_session_token=credentials['SessionToken'],
                            region_name=region)
    else:
        client = boto3.client('ec2', region_name=region)

    if script_config['AWS'].get('skip_stopped', True) == False or script_config['Local'].get('skip_stopped', True) == False or profile.get('skip_stopped', True) == False:
        search_states = ['running', 'pending', 'shutting-down', 'terminated', 'stopping', 'stopped']
    else:
        search_states = ['running']

    response = client.describe_instances(
        Filters=[{
            'Name': 'instance-state-name',
            'Values': search_states
        }
        ]
    )

    vpc_data_all = client.describe_vpcs(
            VpcIds=[]
        )

    if response.get('Reservations',False):
        for reservation in response['Reservations']:
            for instance in reservation['Instances']:
                if script_config["Local"].get('parallel_exec', True):
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(fetchEC2Instance, instance, client, groups, instances, instance_source, reservation, vpc_data_all)
                        return_value = future.result()
                        print(f'{instance_source}: {return_value}')
                else:
                    print(fetchEC2Instance(instance, client, groups, instances, instance_source, reservation, vpc_data_all))
    else:
        print(f'{instance_source}: No instances found in {region}')

def get_MFA_func():
    try:
        retry = 3
        while retry > 0:
            mfa_TOTP = inputimeout(prompt=f"Note: The MFA code must be uniq for each account," \
                                    f' so wait until it rotates before entering it for each account...\n' \
                                    f'Enter your MFA code for "{profile["name"]}", so you can assume the role "{profile["role_arns"][role_arn].rpartition("/")[2]}"' \
                                    f' in "{role_arn}": ',
                                    timeout=30
                                    )
            if (not mfa_TOTP.isnumeric() or len(mfa_TOTP) != 6) and retry > 1:
                print(f"Sorry, MFA can only be 6 numbers.\nPlease try again.")
            elif retry == 1:
                print(f"Maximum amount of failed attempts reached, so skipping {role_arn}.")
                return
            else:
                return mfa_TOTP
            retry -= 1
    except TimeoutOccurred:
        print(f"Input not supplied within allowed amount of time, skipping {role_arn}.")
        return False

def getEC2Instances(profile, role_arn = False):
    groups = {}
    instances = {}
    credentials = False
    global instance_counter

    if isinstance(profile,dict):
        instance_source = "aws." + profile['name']
        profile_name = profile['name']
        boto3.setup_default_session(aws_access_key_id=profile['aws_access_key_id'],aws_secret_access_key=profile['aws_secret_access_key'],region_name="eu-central-1")
    else:
        instance_source = "aws." + profile
        boto3.setup_default_session(profile_name=profile,region_name="eu-central-1")
        profile_name = profile

    if role_arn:
        instance_source = f"{instance_source}.{role_arn}"
        role_session_name = f"{os.path.basename(__file__).rpartition('.')[0]}."\
                            f"{getpass.getuser()}@{platform.uname()[1]}"
        sts_client = boto3.client('sts')
        if profile.get("mfa_serial_number", False):
            retry = 3
            while retry > 0:
                try:
                    assumed_role_object=sts_client.assume_role(
                                        RoleArn=profile["role_arns"][role_arn],
                                        RoleSessionName=role_session_name,
                                        DurationSeconds=3600,
                                        SerialNumber=profile["mfa_serial_number"],
                                        TokenCode=get_MFA_func()
                    )
                    if assumed_role_object['ResponseMetadata']['HTTPStatusCode'] == 200:
                        break
                except:
                    retry -= 1
                    if retry == 0:
                        print(f'Sorry, was unable to "login" to {profile_name} using STS + MFA.')
                        return
                    else:
                        pass
        else:
            try:
                assumed_role_object=sts_client.assume_role(
                                    RoleArn=profile["role_arns"][role_arn],
                                    RoleSessionName=role_session_name
                )
            except:
                print(f"Was unable to assume role. Maybe you need MFA?")
                return

        credentials=assumed_role_object['Credentials']
        client = boto3.client('ec2',
                                aws_access_key_id=credentials['AccessKeyId'],
                                aws_secret_access_key=credentials['SecretAccessKey'],
                                aws_session_token=credentials['SessionToken'])
    else:
        client = boto3.client('ec2')
    instance_counter[instance_source] = 0
    
    try:
        ec2_regions = [region['RegionName'] for region in client.describe_regions()['Regions']]
    except:
        print(f'Was unable to retrive information for "regions" in account "{profile_name}", so it was skipped.')
        return

    if script_config["Local"].get('parallel_exec', True):
        with concurrent.futures.ThreadPoolExecutor() as executor:
            [executor.submit(fetchEC2Region, region , profile_name, instances, groups, instance_source, credentials) for region in ec2_regions]
    else:
        for region in ec2_regions:
            fetchEC2Region(region , profile_name, instances, groups, instance_source, credentials)

    for ip in instances:
        instance = instances[ip]
        instance['name'] = instance['name'] + str(instance['index']) if groups[instance['group']] > 1 else instance['name']
    
    cloud_instances_obj_list.append({"instance_source": instance_source, "groups": groups, "instances":instances})


def flastList(dict_list):
    global instance_counter
    bookmark_counter = 1

    for d in dict_list:
        if not 'instance_by_region' in d:
            d['instance_by_region'] = {}
        for key,instance in d['instances'].items():
            if not instance['region'] in d['instance_by_region']:
                d['instance_by_region'][instance['region']] = []
            instance['ip'] = key
            d['instance_by_region'][instance['region']].append(instance)
    del d


    profiles = "[Bookmarks]\nSubRep=\nImgNum=42"
    
    for profile_dict in dict_list:
        for region in profile_dict['instance_by_region']:
            profiles +=  f'\n[Bookmarks_{bookmark_counter}]\nSubRep={profile_dict["instance_source"]}\\{region}\nImgNum=41\n'
            for instance in profile_dict['instance_by_region'][region]:
                instance_counter[profile_dict['instance_source']] += 1
                shortName = instance['name'].rpartition('.')[2]
                group = instance['group']

                connection_command = f"{shortName}= "

                tags = ["Account: " + profile_dict["instance_source"], str(instance['id'])]
                for tag in instance['iterm_tags']:
                    tags.append(tag)
                if profile_dict["groups"].get(group, 0) > 1:
                    tags.append(group)


                if "Sorry" in instance:
                    connection_command = "echo"
                    ip_for_connection = instance
                elif instance.get('instance_use_ip_public', False) == True or not instance['bastion']:
                    ip_for_connection = instance['ip_public']
                else:
                    ip_for_connection = instance['ip']


                if instance['con_username']:
                    con_username = instance['con_username']
                else:
                    con_username = '<default>'
                
                if instance.get('platform', '') == 'windows':
                    if not instance['con_username']:
                        con_username = "Administrator"
                    connection_type = "#91#4%"
                else:
                    connection_type = "#109#0%"
                
                if instance['bastion'] != False \
                    or ( (instance['instance_use_ip_public'] == True and instance['instance_use_bastion'] == True) \
                    or instance['instance_use_bastion'] == True):
                    
                    bastion_for_profile = instance['bastion']
                else:
                    bastion_for_profile = ''

                if instance['ssh_key'] and instance['use_shared_key']:
                    sharead_key_path = os.path.join(connection_command,os.path.expanduser(script_config["Local"].get('ssh_keys_path', '.')), instance['ssh_key'])
                else:
                        sharead_key_path = ''
                tags = ','.join(tags)
                if instance['bastion_con_port'] != 22:
                    bastion_port = instance['bastion_con_port']
                else:
                    bastion_port = ''
                if instance['bastion_con_username']:
                    bastion_user = instance['bastion_con_username']
                else:
                    bastion_user = ''
                profile =   (
                        f"\n{shortName}= {connection_type}{ip_for_connection}%{instance['con_port']}%"
                        f"{con_username}%%-1%-1%%{bastion_for_profile}%{bastion_port}%{bastion_user}%0%"
                        f"0%0%{sharead_key_path}%%"
                        f"-1%0%0%0%%1080%%0%0%1#MobaFont%10%0%0%0%15%236,"
                        f"236,236%30,30,30%180,180,192%0%-1%0%%xterm%-1%"
                        f"-1%_Std_Colors_0_%80%24%0%1%-1%<none>%%0#0# {tags}\n"
                )
                profiles += profile
            bookmark_counter += 1

    handle = open(os.path.expanduser(os.path.join(OutputDir,'Cloud-profiler-Moba.mxtsessions')),'wt')
    handle.write(profiles)
    handle.close()




def updateMoba(dict_list):
    global instance_counter
    bookmark_counter = 1

    for d in dict_list:
        if not 'instance_by_region' in d:
            d['instance_by_region'] = {}
        for key,instance in d['instances'].items():
            if not instance['region'] in d['instance_by_region']:
                d['instance_by_region'][instance['region']] = []
            instance['ip'] = key
            d['instance_by_region'][instance['region']].append(instance)
    del d


    profiles = "[Bookmarks]\nSubRep=\nImgNum=42"
    
    for profile_dict in dict_list:
        for region in profile_dict['instance_by_region']:
            profiles +=  f'\n[Bookmarks_{bookmark_counter}]\nSubRep={profile_dict["instance_source"]}\\{region}\nImgNum=41\n'
            for instance in profile_dict['instance_by_region'][region]:
                instance_counter[profile_dict['instance_source']] += 1
                shortName = instance['name'].rpartition('.')[2]
                group = instance['group']

                connection_command = f"{shortName}= "

                tags = ["Account: " + profile_dict["instance_source"], str(instance['id'])]
                for tag in instance['iterm_tags']:
                    tags.append(tag)
                if profile_dict["groups"].get(group, 0) > 1:
                    tags.append(group)


                if "Sorry" in instance:
                    connection_command = "echo"
                    ip_for_connection = instance
                elif instance.get('instance_use_ip_public', False) == True or not instance['bastion']:
                    ip_for_connection = instance['ip_public']
                else:
                    ip_for_connection = instance['ip']


                if instance['con_username']:
                    con_username = instance['con_username']
                else:
                    con_username = '<default>'
                
                if instance.get('platform', '') == 'windows':
                    if not instance['con_username']:
                        con_username = "Administrator"
                    connection_type = "#91#4%"
                else:
                    connection_type = "#109#0%"
                
                if instance['bastion'] != False \
                    or ( (instance['instance_use_ip_public'] == True and instance['instance_use_bastion'] == True) \
                    or instance['instance_use_bastion'] == True):
                    
                    bastion_for_profile = instance['bastion']
                else:
                    bastion_for_profile = ''

                if instance['ssh_key'] and instance['use_shared_key']:
                    sharead_key_path = os.path.join(connection_command,os.path.expanduser(script_config["Local"].get('ssh_keys_path', '.')), instance['ssh_key'])
                else:
                        sharead_key_path = ''
                tags = ','.join(tags)
                if instance['bastion_con_port'] != 22:
                    bastion_port = instance['bastion_con_port']
                else:
                    bastion_port = ''
                if instance['bastion_con_username']:
                    bastion_user = instance['bastion_con_username']
                else:
                    bastion_user = ''
                profile =   (
                        f"\n{shortName}= {connection_type}{ip_for_connection}%{instance['con_port']}%"
                        f"{con_username}%%-1%-1%%{bastion_for_profile}%{bastion_port}%{bastion_user}%0%"
                        f"0%0%{sharead_key_path}%%"
                        f"-1%0%0%0%%1080%%0%0%1#MobaFont%10%0%0%0%15%236,"
                        f"236,236%30,30,30%180,180,192%0%-1%0%%xterm%-1%"
                        f"-1%_Std_Colors_0_%80%24%0%1%-1%<none>%%0#0# {tags}\n"
                )
                profiles += profile
            bookmark_counter += 1

    handle = open(os.path.expanduser(os.path.join(OutputDir,'Cloud-profiler-Moba.mxtsessions')),'wt')
    handle.write(profiles)
    handle.close()



def updateTerm(dict_list):
    global instance_counter

    for profile_dict in dict_list:
        profiles = []
        for instance in profile_dict['instances']:
            instance_counter[profile_dict['instance_source']] += 1
            shortName = profile_dict['instances'][instance]['name'][4:]
            group = profile_dict["instances"][instance]['group']

            connection_command = "ssh "

            tags = ["Account: " + profile_dict["instance_source"], instance]
            for tag in profile_dict["instances"][instance]['iterm_tags']:
                tags.append(tag)
            if profile_dict["groups"].get(group, 0) > 1:
                tags.append(group)


            if "Sorry" in instance:
                connection_command = "echo"
                ip_for_connection = instance
            elif profile_dict["instances"][instance].get('instance_use_ip_public', False) == True or not profile_dict["instances"][instance]['bastion']:
                ip_for_connection = profile_dict["instances"][instance]['ip_public']
            else:
                ip_for_connection = instance

            if profile_dict["instances"][instance]['con_username']:
                con_username = profile_dict["instances"][instance]['con_username']

            if profile_dict["instances"][instance].get('platform', '') == 'windows':
                if not profile_dict["instances"][instance]['con_username']:
                    con_username = "Administrator"

            connection_command = f"{connection_command} {ip_for_connection}"
            
            if profile_dict["instances"][instance]['bastion'] != False \
                or ( (profile_dict["instances"][instance]['instance_use_ip_public'] == True and profile_dict["instances"][instance]['instance_use_bastion'] == True) \
                or profile_dict["instances"][instance]['instance_use_bastion'] == True):
                
                bastion_connection_command = ''

                if profile_dict['instances'][instance]['bastion_con_username']:
                    bastion_connection_command =    f"{profile_dict['instances'][instance]['bastion_con_username']}@" \
                                                    f"{profile_dict['instances'][instance]['bastion']}"
                else:
                    bastion_connection_command =    f"{profile_dict['instances'][instance]['bastion']}"
                
                if profile_dict['instances'][instance]['bastion_con_port'] and profile_dict['instances'][instance]['bastion_con_port'] != 22:
                    bastion_connection_command = f"{bastion_connection_command}:{profile_dict['instances'][instance]['bastion_con_port']}"
                
                connection_command = f"{connection_command} -J {bastion_connection_command}"
                
                if profile_dict["instances"][instance].get('platform', '') == 'windows':
                
                    connection_command = f"function random_unused_port {{ local port=$( echo $((2000 + ${{RANDOM}} % 65000))); (echo " \
                                    f">/dev/tcp/127.0.0.1/$port) &> /dev/null ; if [[ $? != 0 ]] ; then export " \
                                    f"RANDOM_PORT=$port; else random_unused_port ;fi }}; " \
                                    f"if [[ -n ${{RANDOM_PORT+x}} && -n \"$( ps aux | grep \"ssh -f\" | grep -v grep | awk \'{{print $2}}\' )\" ]]; " \
                                    f" then kill -9 $( ps aux | grep \"ssh -f\" | grep -v grep | awk \'{{print $2}}\' ) ; else random_unused_port; fi ;ssh -f -o " \
                                    f"ExitOnForwardFailure=yes -L ${{RANDOM_PORT}}:{ip_for_connection}:" \
                                    f"{profile_dict['instances'][instance].get('con_port_windows', 3389)} " \
                                    f"{bastion_connection_command} sleep 10 ; open " \
                                    f"'rdp://full%20address=s:127.0.0.1:'\"${{RANDOM_PORT}}\"'" \
                                    f"&audiomode=i:2&disable%20themes=i:0&screen%20mode%20id=i:1&use%20multimon" \
                                    f":i:0&username:s:{con_username}" \
                                    f"&desktopwidth=i:1024&desktopheight=i:768'"
            elif profile_dict["instances"][instance].get('platform', '') == 'windows':
                connection_command = f"open 'rdp://full%20address=s:{ip_for_connection}:{profile_dict['instances'][instance].get('con_port_windows', 3389)}" \
                                f"&audiomode=i:2&disable%20themes=i:0&screen%20mode%20id=i:1&use%20multimon" \
                                f":i:0&username:s:{con_username}" \
                                f"&desktopwidth=i:1024&desktopheight=i:768'"

            if profile_dict["instances"][instance]['password'][0] and profile_dict["instances"][instance].get('platform', '') == 'windows':
                    connection_command =    f"echo \"\\nThe Windows password on record is:\\n{profile_dict['instances'][instance]['password'][1].rstrip()}\\n\\n\" " \
                                            f"\\;echo -n '{profile_dict['instances'][instance]['password'][1].rstrip()}' | pbcopy; " \
                                            f'echo \"\\nIt has been sent to your clipboard for easy pasting\\n\\n\";{connection_command}'

            elif profile_dict["instances"][instance].get('platform', '') == 'windows':
                    connection_command =    f'echo \"\\nThe Windows password could not be decrypted...\\n' \
                                            f"{str(profile_dict['instances'][instance]['password'][1])}\""

            if profile_dict["instances"][instance].get('platform', '') != 'windows':
                connection_command = f"{connection_command} {script_config['Local']['ssh_base_string']}"

                if profile_dict["instances"][instance]['con_username']:
                    connection_command = f"{connection_command} -l {profile_dict['instances'][instance]['con_username']}"
            
                if profile_dict["instances"][instance]['con_port']:
                    connection_command = f"{connection_command} -p {profile_dict['instances'][instance]['con_port']}"

                if profile_dict["instances"][instance]['ssh_key'] and profile_dict["instances"][instance]['use_shared_key']:
                    connection_command = f"{connection_command} -i {script_config['Local'].get('ssh_keys_path', '.')}/{profile_dict['instances'][instance]['ssh_key']}"
            
            if not profile_dict["instances"][instance]['dynamic_profile_parent_name']:
                dynamic_profile_parent_name = 'Default'
            else:
                dynamic_profile_parent_name = profile_dict["instances"][instance]['dynamic_profile_parent_name']
                
            profile = {"Name":profile_dict["instances"][instance]['name'],
                        "Guid":f"{profile_dict['instance_source']}-{str(profile_dict['instances'][instance]['id'])}",
                        "Badge Text":shortName + '\n' + profile_dict["instances"][instance]['InstanceType'] + '\n' + ip_for_connection,
                        "Tags":tags,
                        "Dynamic Profile Parent Name": dynamic_profile_parent_name,
                        "Custom Command" : "Yes",
                        "Initial Text" : connection_command
                        }

            profiles.append(profile)

        profiles = {"Profiles":(profiles)}
        handle = open(os.path.expanduser(os.path.join(OutputDir,profile_dict["instance_source"])),'wt')
        handle.write(json.dumps(profiles,sort_keys=True,indent=4, separators=(',', ': ')))
        handle.close()

def update_statics():
    profiles =[]
    app_static_profile_handle = open(os.path.expanduser(os.path.join(OutputDir, "statics")),"wt")
    path_to_static_profiles = os.path.expanduser(script_config["Local"]['static_profiles'])
    
    for root, _ , files in os.walk(path_to_static_profiles, topdown=False):
        for name in files:
            if name == '.DS_Store':
                print(f'Static profiles, skipping ".DS_Store"')
                continue
            print(f'Working on static profile: {name}')
            static_profile_handle=open(os.path.join(root, name))
            profiles.append(json.load(static_profile_handle))

    
    profiles = {"Profiles":(profiles)} 
    app_static_profile_handle.write(json.dumps(profiles,sort_keys=True,indent=4, separators=(',', ': ')))
    app_static_profile_handle.close()



# Updates the /etc/hosts file with the EC2 private addresses
# /etc/hosts must include the list of EC2 instances between two lines: the first contains '# AWS EC2' 
# and the last a single # character.
def updateHosts(instances,groups):
    handle = open('/etc/hosts')
    lines = handle.read().splitlines()    
    handle.close()
    state = False

    hout = open('/etc/hosts','wt')

    startDelimiter = "# AWS EC2"
    endDelimiter = "#"

    for line in lines:
        if line == startDelimiter:
            state = True
            continue
        if state == True and line == endDelimiter:
            state = False
            continue
        if not state:
            hout.write(line + "\n")

    hout.write(startDelimiter + "\n")
    for ip in instances:
        instance = instances[ip]
        name = instance['name']
        hout.write(ip + "\t" + name + "\n")
	
    hout.write(endDelimiter + "\n")
    hout.close()


#MAIN
if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument("--list", "-l", help="script should be used to generate a flat list of instances", action="store_true")
    parser.add_argument("--output_file", "-o", help="name of file to output the list to", action="store_false")

    args = parser.parse_args()

    print(f"args: {args}")

    if args.list:
        print(f"args.list is: {args.list}")
    

    instance_counter = {}
    script_path = os.path.abspath(__file__)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cloud_instances_obj_list = []

    # From repo
    with open(os.path.join(script_dir,'config.yaml')) as conf_file:
        script_config_repo = yaml.full_load(conf_file)

    if platform.system() == 'Windows':
        OutputDir = "~/Cloud Profiler/"

    else:
        OutputDir = "~/Library/Application Support/iTerm2/DynamicProfiles/"
    
    if not os.path.isdir(os.path.expanduser(OutputDir)):
        os.makedirs(os.path.expanduser(OutputDir))

    # From user home direcotry
    script_config = {}
    script_config_user = {}
    if os.path.isfile(os.path.expanduser("~/.iTerm-cloud-profile-generator/config.yaml")):
        with open(os.path.expanduser("~/.iTerm-cloud-profile-generator/config.yaml")) as conf_file:
            script_config_user = yaml.full_load(conf_file)
    else:
        if not os.path.isdir(os.path.expanduser("~/.iTerm-cloud-profile-generator/")):
            os.makedirs(os.path.expanduser("~/.iTerm-cloud-profile-generator/"))
        shutil.copy2(os.path.join(script_dir,'config.yaml'), os.path.expanduser("~/.iTerm-cloud-profile-generator/"))


    for key in script_config_repo:
        script_config[key] = {**script_config_repo.get(key, {}),**script_config_user.get(key, {})}


    username = getpass.getuser()
    config = configparser.ConfigParser()



    # AWS profiles iterator
    if script_config['AWS'].get('profiles', False):
        for profile in script_config['AWS']['profiles']:
            print(f"Working on {profile['name']}")
            if isinstance(profile.get("role_arns", False),dict):
                for role_arn in profile["role_arns"]:
                    getEC2Instances(profile, role_arn)
            else:
                getEC2Instances(profile)
            
    # AWS profiles iterator from config file
    if script_config['AWS'].get('use_awscli_profiles', False):
        if os.path.exists(os.path.expanduser(script_config['AWS']['aws_credentials_file'])):
            config.read(os.path.expanduser(script_config['AWS']['aws_credentials_file']))
            for i in config.sections():
                if i not in script_config['AWS']['exclude_accounts']:
                    print(f'Working on AWS profile from credentials file: {i}')
                    getEC2Instances(i)
    
    # DO profiles iterator
    if script_config['DO'].get('profiles', False):
        for profile in script_config['DO']['profiles']:
            print(f"Working on {profile['name']}")
            getDOInstances(profile)
    
    if platform.system() == 'Windows':
        updateMoba(cloud_instances_obj_list)
    else:
        updateTerm(cloud_instances_obj_list)
        # Static profiles iterator
        update_statics()


    print(f"\nCreated profiles {json.dumps(instance_counter,sort_keys=True,indent=4, separators=(',', ': '))}\nTotal: {sum(instance_counter.values())}")
    print(f"\nWe wish you calm clouds and a serene path...\n")