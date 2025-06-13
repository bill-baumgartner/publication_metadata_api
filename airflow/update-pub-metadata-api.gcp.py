from airflow.operators.python_operator import PythonOperator, BranchPythonOperator
from airflow.operators.bash_operator import BashOperator
from airflow.models import DAG
from datetime import datetime, timedelta
from medline_xml_util import wrap_title_and_abstract_with_cdata
import os
import json

# =============================================================================
# | This Airflow workflow updates the Publication Metadata API by processing  |
# | the MEDLINE daily update files.                                           |
# =============================================================================

#====ENVIRONMENT VARIABLES THAT MUST BE SET IN CLOUD COMPOSER====
# Note that Dataflow is not currently used by this pipeline,
# but the environment variable are included in case a DataflowOperator
# is added in the future
DATAFLOW_TMP_LOCATION=os.environ.get('DATAFLOW_TMP_LOCATION')
DATAFLOW_STAGING_LOCATION=os.environ.get('DATAFLOW_STAGING_LOCATION')
DATAFLOW_ZONE=os.environ.get('DATAFLOW_ZONE')
DATAFLOW_REGION=os.environ.get('DATAFLOW_REGION')

TM_PIPELINES_JAR=os.environ.get('TM_PIPELINES_JAR')
TM_PIPELINES_JAR_NAME=os.environ.get('TM_PIPELINES_JAR_NAME')

PUBLICATION_METADATA_BUCKET=os.environ.get('PUBLICATION_METADATA_BUCKET')
PUBLICATION_METADATA_PATH=os.environ.get('PUBLICATION_METADATA_PATH')

PUBLICATION_METADATA_API_URL=os.environ.get('PUBLICATION_METADATA_API_URL')
PUBLICATION_METADATA_SERVICE_HMAC_KEY_ID=os.environ.get('PUBLICATION_METADATA_SERVICE_HMAC_KEY_ID')
PUBLICATION_METADATA_SERVICE_HMAC_SECRET=os.environ.get('PUBLICATION_METADATA_SERVICE_HMAC_SECRET')

#==================DAG ARGUMENTS==============================

args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2023, 10, 6),
     # run this dag daily @ 10pm MT which is 4am UTC
    'schedule_interval': '0 4 * * *',
    'email': ['william.baumgartner@cuanschutz.edu'],
    'email_on_failure': True,
    'retries': 0,
    'retry_delay': timedelta(minutes=2),
    'dataflow_default_options': {
        'zone': DATAFLOW_ZONE,
        'region': DATAFLOW_REGION,
        'stagingLocation': DATAFLOW_STAGING_LOCATION,
        'gcpTempLocation': DATAFLOW_TMP_LOCATION,
    }
}

base_directory = '/home/airflow/gcs/data/pub_metadata_2023'
update_directory = base_directory + '/update_files'
load_directory = base_directory + '/to_load'

dag = DAG(dag_id='update-pub-metadata-api-dag', 
            default_args=args, 
            catchup=False, 
            schedule_interval='0 4 * * *',)

# download only new files using wget and log names of the downloaded files
# to pub-metadata-update-downloaded-files.txt
download = BashOperator(
    task_id='download-pubmed-update-files',
    bash_command=f"cd {update_directory} && " 
    + "wget -N 'ftp://ftp.ncbi.nlm.nih.gov:21/pubmed/updatefiles/pubmed*' "
    + "2>&1 | grep done | grep '.gz' | grep -v '.gz.md5' | tr -s ' ' | "
    + "cut -f 7 -d ' '  > pub-metadata-update-downloaded-files.txt && "
    + "cat pub-metadata-update-downloaded-files.txt",
    dag=dag)

# TODO: the md5sum verify could be targeted to only the newly downloaded files
verify_md5 = BashOperator(
    task_id='verify-md5-checksums',
    bash_command=f"cd {update_directory} && "
    + "if [ -s pub-metadata-update-downloaded-files.txt ]; " 
    + "then ls *.md5 | "
    + "grep -f pub-metadata-update-downloaded-files.txt | "
    + "md5sum --check $(cat); fi",
    dag=dag)

# copy any newly downloaded Medline XML files to the to_load directory where
# they will be processed further
populate_load_directory = BashOperator(
    task_id='populate-load-directory',
    bash_command=f"cd {update_directory} && mkdir -p {load_directory} && "
    + "if [ -s pub-metadata-update-downloaded-files.txt ]; " 
    + "then xargs -a pub-metadata-update-downloaded-files.txt " 
    + f"cp -t {load_directory}; fi",
    dag=dag)

# checks to see if the to_load directory is empty
def check_for_files_to_process(**kwargs):
    load_dir = os.listdir(load_directory)
    if len(load_dir) > 0:
        return 'wrap_with_cdata'
    return 'pipeline_end'

# checks to see if the to_load/ directory is empty. If not empty,
# then returns the id for the create_publication_metadata task.
check_for_files_to_process = BranchPythonOperator(
        task_id='check_for_files_to_process',
        python_callable=check_for_files_to_process,
        provide_context=True,
        dag=dag)

# wraps the article title and abstract text with CDATA fields - this is
# necessary so that the XML parser doesn't fail when it encounters formatting
# elements, such as <b>, <i>, <sup>, etc., in the titles and abstracts.
wrap_with_cdata = PythonOperator(
        task_id='wrap_with_cdata',
        python_callable=wrap_title_and_abstract_with_cdata,
        provide_context=True,
        op_kwargs={'dir': load_directory},
        dag=dag)

# create the publication metadata files
create_publication_metadata = BashOperator(
    task_id='create_publication_metadata',
    bash_command=f"cd {base_directory} && "
    + "mkdir -p metadata && java -version && "
    + "ls -lhrt /home/airflow/gcs/data && "
    + "jar -tf $JAR_PATH | grep MedlineUiMetadataExtractor && "
    + "java -cp $JAR_PATH trnsltr.pub_metadata_api.MedlineUiMetadataExtractor to_load metadata 0",
    env={'JAR_PATH': '/home/airflow/gcs/data/' + TM_PIPELINES_JAR_NAME},
    dag=dag)

# cat all publication metadata into a single file
# we assume that there are things to add but check for the existence of the 
# delete file as there was at least one update file instance without deletes: pubmed24n1470.xml.g 
aggregate_publication_metadata = BashOperator(
    task_id='aggregate_publication_metadata',
    bash_command=f"cd {base_directory}/metadata && "
    + "gunzip -c *.ui_metadata.tsv.gz | grep DOC_ID | sort | uniq > {{ ts_nodash }}.ui_metadata.tsv && "
    + "gunzip -c *.ui_metadata.tsv.gz | grep PMID >> {{ ts_nodash }}.ui_metadata.tsv && "
    + "gzip {{ ts_nodash }}.ui_metadata.tsv && "
    + "touch {{ ts_nodash }}.ui_metadata.delete.tsv && " # create an empty delete file
    + "shopt -s nullglob && for file in *.ui_metadata.delete.tsv.gz; do gunzip -c $file >> {{ ts_nodash }}.ui_metadata.delete.tsv; done && "
    # + "gunzip -c *.ui_metadata.delete.tsv.gz > {{ ts_nodash }}.ui_metadata.delete.tsv && "
    + "gzip {{ ts_nodash }}.ui_metadata.delete.tsv",
    dag=dag)

# copy the aggregate publication metadata file to the proper bucket
# TODO: investigate potential race condition in use of ts_nodash
move_aggregate_publication_data_to_bucket = BashOperator(
    task_id='move_aggregate_publication_data_to_bucket',
    bash_command=f"cd {base_directory}/metadata && "
    + "CLOUDSDK_PYTHON=/usr/bin/python gsutil cp {{ ts_nodash }}.ui_metadata.tsv.gz gs://$PUBLICATION_METADATA_BUCKET/$PUBLICATION_METADATA_PATH && "
    + "CLOUDSDK_PYTHON=/usr/bin/python gsutil cp {{ ts_nodash }}.ui_metadata.delete.tsv.gz gs://$PUBLICATION_METADATA_BUCKET/$PUBLICATION_METADATA_PATH",
    env={'PUBLICATION_METADATA_BUCKET': PUBLICATION_METADATA_BUCKET,
     'PUBLICATION_METADATA_PATH':PUBLICATION_METADATA_PATH},
    dag=dag)

def build_curl_cmd(filename):
    data = {
        'source': {
            'bucket': PUBLICATION_METADATA_BUCKET,
            'filepath': PUBLICATION_METADATA_PATH + '/' + filename,
            'hmac_key_id': PUBLICATION_METADATA_SERVICE_HMAC_KEY_ID,
            'hmac_secret': PUBLICATION_METADATA_SERVICE_HMAC_SECRET
        }
    }
    cmd = f'curl --location --request GET {PUBLICATION_METADATA_API_URL} --header "Content-Type: application/json" --data-raw \'{json.dumps(data)}\' >> {base_directory}/responses/{datetime.now().isoformat().split("T")[0]}.txt'
    print(f'CURLCMD: {cmd}')
    return cmd

# invoke the Publication Metadata API to load the metadata
trigger_publication_metadata_load = BashOperator(
    task_id='trigger_publication_metadata_load',
    bash_command=build_curl_cmd("{{ ts_nodash }}.ui_metadata.tsv.gz"),
    dag=dag)

# invoke the Publication Metadata API to delete medline records
trigger_publication_metadata_delete = BashOperator(
    task_id='trigger_publication_metadata_delete',
    bash_command=build_curl_cmd("{{ ts_nodash }}.ui_metadata.delete.tsv.gz"),
    dag=dag)

# clean up after the pmid file creation process by deleting all files in the ids directory
clean_ids_dir = BashOperator(
    task_id='clean_ids_dir',
    bash_command=f"rm -rf {base_directory}/ids",
    trigger_rule="all_done",
    dag=dag)

# If all upstream tasks succeed, then remove files from the to_load directory.
# If any of the upstream processes failed then the files will be kept in the
# to_load directory so that they are processed the next time the workflow runs.
# 
# Since this rule has the trigger_rule=all_done, it runs regardless of success or fail of the pipeline.
pipeline_end = BashOperator(
    task_id='pipeline_end',
    bash_command=f"rm -f {base_directory}/to_load/* && rm -f {base_directory}/metadata/*",
    trigger_rule="all_done",
    dag=dag)


verify_md5.set_upstream(download)
populate_load_directory.set_upstream(verify_md5)
check_for_files_to_process.set_upstream(populate_load_directory)
wrap_with_cdata.set_upstream(check_for_files_to_process)
clean_ids_dir.set_upstream(wrap_with_cdata)
create_publication_metadata.set_upstream(wrap_with_cdata)
aggregate_publication_metadata.set_upstream(create_publication_metadata)
move_aggregate_publication_data_to_bucket.set_upstream(aggregate_publication_metadata)
trigger_publication_metadata_load.set_upstream(move_aggregate_publication_data_to_bucket)
trigger_publication_metadata_delete.set_upstream(trigger_publication_metadata_load)
pipeline_end.set_upstream(trigger_publication_metadata_delete)
pipeline_end.set_upstream(check_for_files_to_process)